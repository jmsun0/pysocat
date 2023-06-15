#!/bin/python3
import argparse
import asyncio
import base64
import inspect
import json
import logging
import os
import platform
import signal
import socket
import stat
import struct
import sys
import time
import types
import typing
import uuid

import aiohttp
import aiohttp.abc
import aiohttp.web
import yarl

DEFAULT_BUFFER_SIZE = int(os.environ.get("DEFAULT_BUFFER_SIZE", "4096"))


class AbstractReader:
    async def read(self, n: int) -> bytes:
        raise NotImplementedError


class AbstractWriter:
    async def write(self, data: bytes):
        raise NotImplementedError

    async def drain(self):
        raise NotImplementedError

    async def write_eof(self):
        raise NotImplementedError

    async def close(self):
        raise NotImplementedError


class PipeCallback:
    async def __call__(self, reader: AbstractReader, writer: AbstractWriter):
        raise NotImplementedError


class Pipe:
    def __init__(
        self, fork: bool = False, cancel_delay: float = -1, split: bool = False
    ):
        self.fork = fork
        self.cancel_delay = cancel_delay
        self.split = split

    async def open(self, callback: PipeCallback):
        raise NotImplementedError

    def __str__(self) -> str:
        return self.__class__.__name__


class Server:
    def __init__(self):
        ...

    async def run(self):
        raise NotImplementedError

    def __str__(self) -> str:
        return self.__class__.__name__


async def transfer_stream(reader: AbstractReader, writer: AbstractWriter):
    try:
        while True:
            data = await reader.read(DEFAULT_BUFFER_SIZE)
            if len(data) == 0:
                break
            await writer.write(data)
            await writer.drain()
    finally:
        await writer.write_eof()


class TransferListener:
    def on_read(self, data: bytes):
        ...

    def on_read_error(self, e: Exception):
        ...

    def on_write_error(self, e: Exception):
        ...

    def on_exit(self):
        ...


async def transfer_stream_with_listener(
    reader: AbstractReader,
    writer: AbstractWriter,
    listener: TransferListener,
):
    try:
        while True:
            try:
                data = await reader.read(DEFAULT_BUFFER_SIZE)
            except Exception as e:
                listener.on_read_error(e)
                raise
            listener.on_read(data)
            if len(data) == 0:
                break
            try:
                await writer.write(data)
                await writer.drain()
            except Exception as e:
                listener.on_write_error(e)
                print(writer.writer)
                raise
    except Exception as e:
        ...
    finally:
        try:
            await writer.write_eof()
        except Exception as e:
            listener.on_write_error(e)

        listener.on_exit()


class ConnectPipeTransferListener(TransferListener):
    def __init__(self, pipe_name):
        super().__init__()
        self.pipe_name = pipe_name
        self.last_read_time = time.time()
        # self.read_error = False
        # self.write_error = False
        # self.exit_event = asyncio.Event()

    def on_read(self, data: bytes):
        self.last_read_time = time.time()
        logging.debug("%s read %d bytes", self.pipe_name, len(data))

    def on_read_error(self, e: Exception):
        # self.read_error = True
        logging.debug("%s read error", self.pipe_name, exc_info=True)

    def on_write_error(self, e: Exception):
        # self.write_error = True
        logging.debug("%s write error", self.pipe_name, exc_info=True)

    def on_exit(self):
        # self.exit_event.set()
        logging.debug("%s transfer exit", self.pipe_name)


async def cancel_and_wait(task: asyncio.Task):
    if task is not None:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logging.debug("Task %s canceled", task.get_name())


async def wait_first_complete_and_cancel_pending(*tasks):
    tasks = list(tasks)
    for i in range(len(tasks)):
        if not asyncio.isfuture(tasks[i]):
            tasks[i] = asyncio.create_task(tasks[i])
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        await cancel_and_wait(task)
    for task in done:
        await task


async def process_pipe_streams(
    pipe1: Pipe,
    pipe2: Pipe,
    reader1: AbstractReader,
    writer1: AbstractWriter,
    reader2: AbstractReader,
    writer2: AbstractWriter,
):
    listener1 = ConnectPipeTransferListener(str(pipe1))
    listener2 = ConnectPipeTransferListener(str(pipe2))
    task1 = asyncio.create_task(
        transfer_stream_with_listener(reader1, writer2, listener1)
    )
    task2 = asyncio.create_task(
        transfer_stream_with_listener(reader2, writer1, listener2)
    )

    task_infos = [(pipe1, task1, listener1), (pipe2, task2, listener2)]
    has_cancel_delay = pipe1.cancel_delay > 0 or pipe2.cancel_delay > 0
    tasks = [info[1] for info in task_infos]

    try:
        if has_cancel_delay:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for i in range(len(task_infos)):
                pipe, task, listener = task_infos[i]
                if task.done() or pipe.cancel_delay <= 0:
                    continue
                while True:
                    wait_time = (
                        listener.last_read_time + pipe.cancel_delay - time.time()
                    )
                    if wait_time < 0:
                        task.cancel()
                        break
                    await asyncio.sleep(wait_time + 0.01)

        await asyncio.wait(tasks)
    except Exception as e:
        logging.debug(e, exc_info=True)
    finally:
        # for writer in [writer1, writer2]:
        #     try:
        #         await writer.close()
        #     except Exception as e:
        #         logging.debug(e, exc_info=True)
        logging.debug("%s,%s finishd", pipe1, pipe2)


class FilterPipe(Pipe):
    def __init__(self, parent: Pipe = None, **kwargs):
        super().__init__(
            fork=parent.fork,
            cancel_delay=parent.cancel_delay,
            split=parent.split,
            **kwargs,
        )
        self.parent = parent

    def __str__(self) -> str:
        return f"{self.__class__.__name__}[{self.parent}]"


class CallOncePipe(FilterPipe):
    def __init__(self, parent: Pipe = None, **kwargs):
        super().__init__(parent=parent, **kwargs)

    async def open(self, callback: PipeCallback):
        is_running = False
        complete_event = asyncio.Event()

        async def new_callback(reader: AbstractReader, writer: AbstractWriter):
            nonlocal is_running
            if is_running:
                await writer.close()
                return
            is_running = True
            try:
                await callback(reader, writer)
            finally:
                complete_event.set()

        pipe_task = asyncio.create_task(self.parent.open(new_callback))

        await wait_first_complete_and_cancel_pending(pipe_task, complete_event.wait())


async def open_split_pipe(pipe: Pipe, passive: bool, split_callback):
    async def callback(reader: AbstractReader, writer: AbstractWriter):
        if passive:

            class SplitPipe(FilterPipe):
                def __init__(self, **kwargs):
                    super().__init__(parent=pipe, **kwargs)

                async def open(self, callback: PipeCallback):
                    split_stream = SplitStream(reader, writer, callback)
                    await split_stream.run_forever()

            split_pipe = SplitPipe()
            await split_callback(split_pipe)
        else:
            split_stream = SplitStream(reader, writer, None)

            class SplitPipe(FilterPipe):
                def __init__(self, **kwargs):
                    super().__init__(parent=pipe, **kwargs)

                async def open(self, callback: PipeCallback):
                    reader, writer = await split_stream.create_session()
                    await callback(reader, writer)

            split_stream_run_task = asyncio.create_task(split_stream.run_forever())
            split_pipe = SplitPipe()
            split_callback_task = asyncio.create_task(split_callback(split_pipe))
            await wait_first_complete_and_cancel_pending(
                split_stream_run_task, split_callback_task
            )

    await pipe.open(callback)


async def open_pipe(pipe: Pipe, callback: PipeCallback):
    try:
        await pipe.open(callback)
    except Exception:
        logging.debug("%s open fail", pipe, exc_info=True)
    finally:
        ...


async def connect_pipes_real(pipe1: Pipe, pipe2: Pipe):
    async def callback1(reader1: AbstractReader, writer1: AbstractWriter):
        logging.debug("%s connected", pipe1)

        async def callback2(reader2: AbstractReader, writer2: AbstractWriter):
            logging.debug("%s connected", pipe2)

            await process_pipe_streams(pipe1, pipe2, reader1, writer1, reader2, writer2)

        await open_pipe(pipe2, callback2)

    await open_pipe(pipe1, callback1)


async def connect_pipes(pipe1: Pipe, pipe2: Pipe):
    if pipe2.fork or pipe2.split and not pipe1.fork:
        pipe1, pipe2 = pipe2, pipe1

    pipes = [pipe1, pipe2]
    pipe_forks = [p.fork for p in pipes]
    pipe_splits = [p.split for p in pipes]

    for i in range(len(pipes)):
        call_once = not pipe_forks[i]
        if call_once:
            pipes[i] = CallOncePipe(pipes[i])

    split_count = sum([1 for p in pipe_splits if p])
    assert split_count < 2

    if split_count == 1:
        split_index = pipe_splits.index(True)
        another_fork = pipe_forks[1 - split_index]
        passive = not another_fork

        async def split_callback(split_pipe: Pipe):
            new_pipes = pipes.copy()
            new_pipes[split_index] = split_pipe
            await connect_pipes_real(*new_pipes)

        await open_split_pipe(pipes[split_index], passive, split_callback)
    else:
        await connect_pipes_real(*pipes)


SPLIT_MSG_FLAG_SESSION_CREATE = 1
SPLIT_MSG_FLAG_SESSION_CLOSE = 2
SPLIT_MSG_FLAG_DATA_SEND = 3


class SplitStream:
    def __init__(
        self, reader: AbstractReader, writer: AbstractWriter, callback: PipeCallback
    ):
        self.reader = reader
        self.writer = writer
        self.callback = callback
        self.session_map = {}

    async def write_split_msg(self, msg_flag: int, session_id: str, body: bytes = b""):
        header = {"msg_flag": msg_flag, "session_id": session_id}
        header_bytes = json.dumps(header).encode()

        size_info_fmt = "II"
        size_info_bytes = struct.pack(size_info_fmt, len(header_bytes), len(body))

        await self.writer.write(size_info_bytes)
        await self.writer.write(header_bytes)
        if len(body) > 0:
            await self.writer.write(body)
        await self.writer.drain()

    async def create_session(self) -> "tuple(AbstractReader,AbstractWriter)":
        session_id = str(uuid.uuid4())

        logging.debug("Create session [%s]", session_id)
        reader, peer_writer = create_directly_connected_streams()
        self.session_map[session_id] = peer_writer
        writer = SplitSubStreamWriter(self, session_id)

        await self.write_split_msg(SPLIT_MSG_FLAG_SESSION_CREATE, session_id)
        return reader, writer

    async def on_create_session(self, session_id):
        logging.debug("Peer create session [%s]", session_id)
        reader, peer_writer = create_directly_connected_streams()
        self.session_map[session_id] = peer_writer
        writer = SplitSubStreamWriter(self, session_id)

        loop = asyncio.get_event_loop()

        if self.callback is not None:
            loop.call_soon(lambda: asyncio.ensure_future(self.callback(reader, writer)))

    async def close_session(self, session_id):
        await self.write_split_msg(SPLIT_MSG_FLAG_SESSION_CLOSE, session_id)
        await self.on_close_session(session_id)

    async def on_close_session(self, session_id):
        if session_id in self.session_map:
            logging.debug("Close session [%s]", session_id)
            peer_writer: AbstractWriter = self.session_map[session_id]
            try:
                await peer_writer.write_eof()
            except Exception as e:
                logging.debug(e, exc_info=True)
            try:
                await peer_writer.close()
            except Exception as e:
                logging.debug(e, exc_info=True)
            del self.session_map[session_id]

    async def send_data(self, session_id, data):
        await self.write_split_msg(SPLIT_MSG_FLAG_DATA_SEND, session_id, data)

    async def on_receive_data(self, session_id, data):
        peer_writer: AbstractWriter = self.session_map[session_id]
        if len(data) == 0:
            await peer_writer.write_eof()
        else:
            await peer_writer.write(data)

    async def run_forever(self):
        try:
            buffer = b""
            size_info_fmt = "II"
            size_info_size = struct.calcsize(size_info_fmt)
            while True:
                data = await self.reader.read(DEFAULT_BUFFER_SIZE)
                logging.debug("Split pipe read %d bytes", len(data))
                if len(data) == 0:
                    break
                buffer += data

                off = 0
                while True:
                    t_off = off
                    if t_off + size_info_size > len(buffer):
                        break
                    size_info_bytes = buffer[t_off : t_off + size_info_size]
                    t_off += size_info_size
                    size_info = struct.unpack(size_info_fmt, size_info_bytes)
                    header_size, body_size = size_info
                    if t_off + header_size + body_size > len(buffer):
                        break
                    header_bytes = buffer[t_off : t_off + header_size]
                    t_off += header_size
                    body = buffer[t_off : t_off + body_size]
                    t_off += body_size
                    header = json.loads(header_bytes)
                    try:
                        await self.handle_split_msg(header, body)
                    except Exception as exc:
                        logging.debug(exc, exc_info=True)
                    off = t_off

                buffer = buffer[off:]
        finally:
            for session_id in list(self.session_map.keys()):
                await self.on_close_session(session_id)

    async def handle_split_msg(self, header: dict, body: bytes):
        msg_flag = header["msg_flag"]
        session_id = header["session_id"]

        if msg_flag == SPLIT_MSG_FLAG_SESSION_CREATE:
            await self.on_create_session(session_id)
        elif msg_flag == SPLIT_MSG_FLAG_SESSION_CLOSE:
            await self.on_close_session(session_id)
        elif msg_flag == SPLIT_MSG_FLAG_DATA_SEND:
            await self.on_receive_data(session_id, body)
        else:
            raise ValueError(f"Unknown message flag: {msg_flag}")


class SplitSubStreamWriter(AbstractWriter):
    def __init__(self, split_stream: SplitStream, session_id: str):
        self.split_stream = split_stream
        self.session_id = session_id

    async def write(self, data: bytes):
        await self.split_stream.send_data(self.session_id, data)

    async def drain(self):
        ...

    async def write_eof(self):
        return await self.write(b"")

    async def close(self):
        await self.split_stream.close_session(self.session_id)


last_tasks = None


def record_tasks():
    global last_tasks
    last_tasks = asyncio.all_tasks()


def diff_tasks():
    current_tasks = asyncio.all_tasks()
    last_task_map = {k.get_name(): k for k in last_tasks}
    current_task_map = {k.get_name(): k for k in current_tasks}
    logging.debug("diff_tasks==================================================")
    for name in current_task_map:
        if name not in last_task_map:
            logging.debug(current_task_map[name])
    logging.debug("diff_tasks==================================================")


class AIOCoreStreamReader(AbstractReader):
    def __init__(self, reader: asyncio.StreamReader):
        self.reader = reader

    async def read(self, n: int) -> bytes:
        return await self.reader.read(n)


class AIOCoreStreamWriter(AbstractWriter):
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer

    async def write(self, data: bytes):
        self.writer.write(data)

    async def drain(self):
        await self.writer.drain()

    async def write_eof(self):
        if self.writer.can_write_eof():
            self.writer.write_eof()

    async def close(self):
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except NotImplementedError:
            ...


class AIOHttpStreamReader(AbstractReader):
    def __init__(self, reader: aiohttp.StreamReader):
        self.reader = reader

    async def read(self, n: int) -> bytes:
        return await self.reader.read(n)


class AIOHttpStreamWriter(AbstractWriter):
    def __init__(self, writer: aiohttp.abc.AbstractStreamWriter):
        self.writer = writer

    async def write(self, data: bytes):
        await self.writer.write(data)

    async def drain(self):
        await self.writer.drain()

    async def write_eof(self):
        await self.writer.write_eof()

    async def close(self):
        ...


class TCPPipe(Pipe):
    def __init__(
        self, connect_host: str = "0.0.0.0", connect_port: int = None, **kwargs
    ):
        super().__init__(**kwargs)
        self.connect_host = connect_host
        self.connect_port = connect_port

    async def open(self, callback: PipeCallback):
        reader, writer = await asyncio.open_connection(
            self.connect_host, self.connect_port
        )
        reader, writer = AIOCoreStreamReader(reader), AIOCoreStreamWriter(writer)
        await callback(reader, writer)


class TCPServerPipe(Pipe):
    def __init__(
        self,
        bind_host: str = "0.0.0.0",
        bind_port: int = None,
        reuseaddr: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.reuseaddr = reuseaddr

    async def open(self, callback: PipeCallback):
        async def handle_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ):
            reader, writer = AIOCoreStreamReader(reader), AIOCoreStreamWriter(writer)
            await callback(reader, writer)

        async with await asyncio.start_server(
            handle_client,
            self.bind_host,
            self.bind_port,
            reuse_address=self.reuseaddr,
        ) as server:
            await server.serve_forever()


class UnixPipe(Pipe):
    def __init__(self, path: str = None, **kwargs):
        super().__init__(**kwargs)
        self.path = path

    async def open(self, callback: PipeCallback):
        socket_already_exists = os.path.exists(self.path)
        reader, writer = await asyncio.open_unix_connection(self.path)
        try:
            reader, writer = AIOCoreStreamReader(reader), AIOCoreStreamWriter(writer)
            await callback(reader, writer)
        finally:
            if not socket_already_exists and os.path.exists(self.path):
                os.remove(self.path)


class UnixServerPipe(Pipe):
    def __init__(self, path: str = None, **kwargs):
        super().__init__(**kwargs)
        self.path = path

    async def open(self, callback: PipeCallback):
        async def handle_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ):
            reader, writer = AIOCoreStreamReader(reader), AIOCoreStreamWriter(writer)
            await callback(reader, writer)

        socket_already_exists = os.path.exists(self.path)
        try:
            async with await asyncio.start_unix_server(
                handle_client, self.path
            ) as server:
                await server.serve_forever()
        finally:
            if not socket_already_exists and os.path.exists(self.path):
                os.remove(self.path)


class UDPPipe(Pipe):
    def __init__(
        self, connect_host: str = "0.0.0.0", connect_port: int = None, **kwargs
    ):
        super().__init__(**kwargs)
        self.connect_host = connect_host
        self.connect_port = connect_port

    async def open(self, callback: PipeCallback):
        reader, writer = await open_udp_connection(self.connect_host, self.connect_port)
        await callback(reader, writer)


class UDPServerPipe(Pipe):
    def __init__(
        self,
        bind_host: str = "0.0.0.0",
        bind_port: int = None,
        reuseaddr: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.reuseaddr = reuseaddr

    async def open(self, callback: PipeCallback):
        async def handle_client(reader: AbstractReader, writer: AbstractWriter):
            await callback(reader, writer)

        await start_udp_server(
            handle_client,
            self.bind_host,
            self.bind_port,
            reuse_address=self.reuseaddr,
        )


class UDPStreamWriter(AbstractWriter):
    def __init__(
        self,
        transport: asyncio.DatagramTransport,
        remote_addr: "tuple[str,int]",
        close_transport=False,
    ):
        self.transport = transport
        self.remote_addr = remote_addr
        self.close_transport = close_transport
        self.buffer = b""
        self.buffer_size = DEFAULT_BUFFER_SIZE

    async def write(self, data: bytes):
        self.buffer += data
        if len(self.buffer) >= self.buffer_size:
            await self.drain()

    async def drain(self):
        if len(self.buffer) > 0:
            self.transport.sendto(self.buffer, self.remote_addr)
            self.buffer = b""

    async def write_eof(self):
        ...

    async def close(self):
        if self.close_transport:
            self.transport.close()


class UDPServerProtocol:
    def __init__(self, loop: asyncio.AbstractEventLoop, handler):
        self.loop = loop
        self.handler = handler
        self.session_map = {}

    def connection_made(self, transport: asyncio.DatagramTransport):
        logging.debug("UDP server connection made")
        self.transport = transport

    def connection_lost(self, exc):
        logging.debug("UDP server connection lost")

    def datagram_received(self, data: bytes, addr: "tuple[str,int]"):
        if addr not in self.session_map:
            reader, peer_writer = create_directly_connected_streams()
            writer = UDPStreamWriter(self.transport, addr)
            self.session_map[addr] = peer_writer

            self.loop.call_soon(
                lambda: asyncio.ensure_future(self.handler(reader, writer))
            )

        peer_writer: AbstractWriter = self.session_map[addr]

        self.loop.call_soon(lambda: asyncio.ensure_future(peer_writer.write(data)))


async def start_udp_server(handler, host, port, reuse_address=False, reuse_port=True):
    loop = asyncio.get_event_loop()
    udp_protocol = UDPServerProtocol(loop, handler)
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: udp_protocol,
        local_addr=(host, port),
        # reuse_address=reuse_address,
        # reuse_port=reuse_port,
    )
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        transport.close()


class UDPClientProtocol:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.reader: AbstractReader = None
        self.writer: AbstractWriter = None
        self.peer_writer: AbstractWriter = None
        self.connection_made_event = asyncio.Event()

    def connection_made(self, transport):
        logging.debug("UDP client connection made")
        self.transport = transport

        self.reader, self.peer_writer = create_directly_connected_streams()
        self.writer = UDPStreamWriter(self.transport, None, close_transport=True)

        self.connection_made_event.set()

    def datagram_received(self, data, addr):
        self.loop.call_soon(lambda: asyncio.ensure_future(self.peer_writer.write(data)))

    def error_received(self, exc):
        logging.debug("UDP client error received")
        self.loop.call_soon(lambda: asyncio.ensure_future(self.peer_writer.write_eof()))

    def connection_lost(self, exc):
        logging.debug("UDP client connection lost")


async def open_udp_connection(host, port):
    loop = asyncio.get_event_loop()
    udp_protocol = UDPClientProtocol(loop)
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: udp_protocol, remote_addr=(host, port)
    )

    await udp_protocol.connection_made_event.wait()
    return udp_protocol.reader, udp_protocol.writer


async def file_to_reader(file):
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    reader_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: reader_protocol, file)
    reader = AIOCoreStreamReader(reader)
    return reader


async def file_to_writer(file):
    loop = asyncio.get_event_loop()
    writer_transport, writer_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, file
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, None, loop)
    writer = AIOCoreStreamWriter(writer)
    return writer


def is_regular_file(fd):
    return stat.S_ISREG(os.fstat(fd).st_mode)


class BinaryIOReader(AbstractReader):
    def __init__(self, io: typing.BinaryIO):
        self.io = io

    async def read(self, n: int) -> bytes:
        return self.io.read(n)


class BinaryIOWriter(AbstractWriter):
    def __init__(self, io: typing.BinaryIO):
        self.io = io

    async def write(self, data: bytes):
        self.io.write(data)

    async def drain(self):
        self.io.flush()

    async def write_eof(self):
        ...

    async def close(self):
        self.io.close()


STDIO_HOLDER = None


class StdIOPipe(Pipe):
    def __init__(self, cancel_delay: float = 0.2, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cancel_delay = cancel_delay

    async def open(self, callback: PipeCallback):
        global STDIO_HOLDER
        if STDIO_HOLDER is None:
            if is_regular_file(sys.stdin.fileno()):
                reader = BinaryIOReader(sys.stdin.buffer)
            else:
                reader = await file_to_reader(sys.stdin)

            if is_regular_file(sys.stdout.fileno()):
                writer = BinaryIOWriter(sys.stdout.buffer)
            else:
                writer = await file_to_writer(sys.stdout)

            async def close():
                ...

            async def write_eof():
                ...

            writer.close = close
            writer.write_eof = write_eof

            STDIO_HOLDER = reader, writer
        else:
            reader, writer = STDIO_HOLDER

        await callback(reader, writer)


def fd_exists(fd):
    try:
        os.stat(fd)
        return True
    except OSError:
        return False


class ExecPipe(Pipe):
    def __init__(
        self,
        cmd: str = None,
        shell: bool = False,
        fdin: int = None,
        fdout: int = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.cmd = cmd
        self.shell = shell
        self.fdin = fdin
        self.fdout = fdout

    async def open(self, callback: PipeCallback):
        if self.shell:
            args = [self.cmd]
        else:
            args = split_outside_quotes(self.cmd, " ")
        kwargs = dict(
            # stderr=asyncio.subprocess.PIPE,
        )
        pass_fds = []

        if self.fdin is None:
            kwargs.update(stdin=asyncio.subprocess.PIPE)
        else:
            in_read_fd, in_write_fd = os.pipe()
            assert not fd_exists(self.fdin), f"fd {self.fdin} exists"
            os.dup2(in_read_fd, self.fdin)
            in_write_file = os.fdopen(in_write_fd)
            writer = await file_to_writer(in_write_file)
            pass_fds.append(self.fdin)

        if self.fdout is None:
            kwargs.update(stdout=asyncio.subprocess.PIPE)
        else:
            out_read_fd, out_write_fd = os.pipe()
            assert not fd_exists(self.fdout), f"fd {self.fdout} exists"
            os.dup2(out_write_fd, self.fdout)
            out_read_file = os.fdopen(out_read_fd)
            reader = await file_to_reader(out_read_file)
            pass_fds.append(self.fdout)

        if len(pass_fds) != 0:
            kwargs.update(pass_fds=pass_fds)
            kwargs.update(close_fds=True)

        if self.shell:
            proc = await asyncio.create_subprocess_shell(*args, **kwargs)
        else:
            proc = await asyncio.create_subprocess_exec(*args, **kwargs)

        if self.fdin is None:
            writer = AIOCoreStreamWriter(proc.stdin)

        if self.fdout is None:
            reader = AIOCoreStreamReader(proc.stdout)

        async def wait_and_close_pipe():
            status = await proc.wait()
            logging.debug("Subprocess exit status: %d", status)

            try:
                if self.fdin is not None:
                    # in_write_file.close()
                    os.close(in_read_fd)
                    os.close(self.fdin)
            except Exception as e:
                logging.debug(e, exc_info=True)

            try:
                if self.fdout is not None:
                    # out_read_file.close()
                    os.close(out_write_fd)
                    os.close(self.fdout)
            except Exception as e:
                logging.debug(e, exc_info=True)

            logging.debug(f"Subprocess FD closed")

        await asyncio.wait(
            [
                callback(reader, writer),
                wait_and_close_pipe(),
            ]
        )


class StreamReaderPayload(aiohttp.Payload):
    def __init__(self, reader: AbstractReader, *args, **kwargs):
        if "content_type" not in kwargs:
            kwargs["content_type"] = "application/octet-stream"

        super().__init__(reader, *args, **kwargs)

        self.reader = reader

    async def write(self, writer: aiohttp.abc.AbstractStreamWriter):
        if self.reader:
            while True:
                try:
                    data = await self.reader.read(DEFAULT_BUFFER_SIZE)
                except Exception:
                    data = b""
                if len(data) == 0:
                    break
                await writer.write(data)
                await writer.drain()
            await writer.write_eof()
            self.reader = None


class MemoryProtocol(asyncio.Protocol):
    def __init__(self) -> None:
        self._closed_event = asyncio.Event()

    async def _drain_helper(self) -> None:
        ...

    async def _get_close_waiter(self, writer):
        ...

    @property
    async def _closed(self) -> None:
        await self._closed_event.wait()


class MemoryWriteTransport(asyncio.WriteTransport):
    def __init__(self, reader: asyncio.StreamReader, extra=None) -> None:
        self._is_closing = False
        self._reader = reader
        super().__init__(extra)

    def close(self) -> None:
        self._is_closing = True

    def is_closing(self) -> bool:
        return self._is_closing

    def write(self, data: bytes) -> None:
        self._reader.feed_data(data)

    def write_eof(self) -> None:
        self._is_closing = True
        self._reader.feed_eof()

    def can_write_eof(self) -> bool:
        return True

    def abort(self) -> None:
        self._is_closing = True


def create_directly_connected_streams():
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    transport = MemoryWriteTransport(reader, extra={})
    protocol = MemoryProtocol()
    writer = asyncio.StreamWriter(transport, protocol, reader, loop=loop)
    reader = AIOCoreStreamReader(reader)
    writer = AIOCoreStreamWriter(writer)
    return reader, writer


def process_http_headers(headers: dict, push_rules: str):
    if headers is None:
        headers = {}
    if push_rules is not None:
        if push_rules.startswith("@"):
            with open(push_rules[1:], "r") as f:
                push_rules = f.read()
        headers[PUSH_RULES_HEADER_NAME] = base64_encode(push_rules)
    return headers


class HttpPipe(Pipe):
    def __init__(
        self,
        url: str = None,
        method: str = "POST",
        headers: dict = {},
        push_rules: str = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.url = url
        self.method = method
        self.headers = process_http_headers(headers, push_rules)

    async def open(self, callback: PipeCallback):
        async with aiohttp.ClientSession() as http:
            reader, writer = create_directly_connected_streams()
            async with http.request(
                self.method,
                self.url,
                headers=self.headers,
                chunked=True,
                timeout=None,
                data=StreamReaderPayload(reader),
            ) as response:
                response.headers
                if response.status != 200:
                    message_bytes = await response.content.read()
                    raise Exception(
                        f"response.status={response.status},message={message_bytes.decode()}"
                    )
                await callback(
                    AIOHttpStreamReader(response.content),
                    writer,
                )


class AIOWebsocketStreamWriter(AbstractWriter):
    def __init__(
        self, ws: "aiohttp.ClientWebSocketResponse | aiohttp.web.WebSocketResponse"
    ):
        self.ws = ws
        self.buffer = b""

    async def write(self, data: bytes):
        self.buffer += data
        if len(self.buffer) >= DEFAULT_BUFFER_SIZE:
            await self.drain()

    async def drain(self):
        if len(self.buffer) > 0:
            await self.ws.send_bytes(self.buffer)
            self.buffer = b""

    async def write_eof(self):
        await self.ws.send_bytes(b"")

    async def close(self):
        ...


def get_websocket_streams(
    ws: "aiohttp.ClientWebSocketResponse | aiohttp.web.WebSocketResponse",
):
    reader, writer = create_directly_connected_streams()

    async def transfer():
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if len(msg.data) == 0:
                        break
                    await writer.write(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    ...
                else:
                    logging.debug("Unknown msg type %s", msg.type)
        finally:
            logging.debug("Websocket transfer exit")
            await writer.write_eof()
            await writer.close()

    transfer_task = asyncio.create_task(transfer())
    return transfer_task, reader, AIOWebsocketStreamWriter(ws)


class WebsocketPipe(Pipe):
    def __init__(
        self, url: str = None, headers: dict = {}, push_rules: str = None, **kwargs
    ):
        super().__init__(**kwargs)
        self.url = url
        self.headers = process_http_headers(headers, push_rules)

    async def open(self, callback: PipeCallback):
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(
                self.url,
                headers=self.headers,
                autoclose=False,
                autoping=True,
                timeout=None,
                receive_timeout=None,
            ) as ws:
                transfer_task, reader, writer = get_websocket_streams(ws)
                await wait_first_complete_and_cancel_pending(
                    callback(reader, writer), transfer_task
                )


async def run_aiohttp_app(bind_host, bind_port, app: aiohttp.web.Application):
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(
        runner, host=bind_host, port=bind_port, reuse_address=True
    )
    await site.start()

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


async def run_aiohttp_server(
    bind_host, bind_port, path_handlers: "list[tuple[str,str]]", **kwargs
):
    app = aiohttp.web.Application()
    app.update(kwargs)
    for path, handler in path_handlers:
        app.router.add_route("*", path, handler)

    await run_aiohttp_app(bind_host, bind_port, app)


class HttpServerPipe(Pipe):
    def __init__(
        self,
        bind_host: str = "0.0.0.0",
        bind_port: int = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.bind_host = bind_host
        self.bind_port = bind_port

    async def open(self, callback: PipeCallback):
        async def handler(request: aiohttp.web.Request):
            response = aiohttp.web.StreamResponse(headers={}, status=200)
            writer = await response.prepare(request)
            reader = request.content
            reader, writer = AIOHttpStreamReader(reader), AIOHttpStreamWriter(writer)
            await callback(reader, writer)
            return response

        await run_aiohttp_server(
            self.bind_host, self.bind_port, [("/{name:.*}", handler)]
        )


class WebsocketServerPipe(Pipe):
    def __init__(
        self,
        bind_host: str = "0.0.0.0",
        bind_port: int = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.bind_host = bind_host
        self.bind_port = bind_port

    async def open(self, callback: PipeCallback):
        async def handler(request: aiohttp.web.Request):
            response = aiohttp.web.WebSocketResponse()
            await response.prepare(request)
            transfer_task, reader, writer = get_websocket_streams(response)
            await wait_first_complete_and_cancel_pending(
                callback(reader, writer), transfer_task
            )
            return response

        await run_aiohttp_server(
            self.bind_host, self.bind_port, [("/{name:.*}", handler)]
        )


def ioctl(fd: int, flags: str, data: bytes):
    import fcntl

    fcntl.ioctl(fd, flags, data)


def create_tun(dev_name: str, tun_type: str):
    IFF_TUN = 0x1
    IFF_TAP = 0x2
    IFF_NO_PI = 0x1000
    TUNSETIFF = 0x400454CA
    tun_fd = os.open("/dev/net/tun", os.O_RDWR)
    if tun_type == "tun":
        flags = IFF_TUN
    elif tun_type == "tap":
        flags = IFF_TAP
    else:
        raise
    flags = flags | IFF_NO_PI
    ifreq = struct.pack("16sH22s", dev_name.encode(), flags, b"")
    ioctl(tun_fd, TUNSETIFF, ifreq)
    return tun_fd


def socket_ioctl(flags: str, data: bytes):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        ioctl(sock, flags, data)


def set_nic_ip_address(dev_name: str, ip: str, peer=False):
    SIOCSIFADDR = 0x8916
    SIOCSIFDSTADDR = 0x8918
    ip = socket.inet_aton(ip)
    ifreq = struct.pack("16sH2s4s16s", dev_name.encode(), socket.AF_INET, b"", ip, b"")
    if peer:
        ioctl_flags = SIOCSIFDSTADDR
    else:
        ioctl_flags = SIOCSIFADDR
    socket_ioctl(ioctl_flags, ifreq)


def set_nic_netmask(dev_name: str, mask: str):
    SIOCSIFNETMASK = 0x891C
    mask = socket.inet_aton(mask)
    ifreq = struct.pack(
        "16sH2s4s16s", dev_name.encode(), socket.AF_INET, b"", mask, b""
    )
    ioctl_flags = SIOCSIFNETMASK
    socket_ioctl(ioctl_flags, ifreq)


def set_nic_up(dev_name: str):
    IFF_UP = 0x1
    IFF_RUNNING = 0x40
    SIOCSIFFLAGS = 0x8914
    flags = IFF_UP | IFF_RUNNING
    ifreq = struct.pack("16sH22s", dev_name.encode(), flags, b"")
    ioctl_flags = SIOCSIFFLAGS
    socket_ioctl(ioctl_flags, ifreq)


def add_route(rt_gateway_addr, rt_dst_addr, rt_genmask_addr):
    RTF_UP = 0x1
    RTF_GATEWAY = 0x2
    SIOCADDRT = 0x890B
    rt_gateway_family = socket.AF_INET
    rt_gateway_addr = socket.inet_aton(rt_gateway_addr)
    rt_dst_family = socket.AF_INET
    rt_dst_addr = socket.inet_aton(rt_dst_addr)
    rt_genmask_family = socket.AF_INET
    rt_genmask_addr = socket.inet_aton(rt_genmask_addr)
    rt_flags = RTF_UP | RTF_GATEWAY

    _ = b""
    route_data = struct.pack(
        "8sH2s4s8sH2s4s8sH2s4s8sH6s56s",
        _,
        rt_gateway_family,
        _,
        rt_gateway_addr,
        _,
        rt_dst_family,
        _,
        rt_dst_addr,
        _,
        rt_genmask_family,
        _,
        rt_genmask_addr,
        _,
        rt_flags,
        _,
        _,
    )
    socket_ioctl(SIOCADDRT, route_data)


def init_tun(
    tun_name: str = "tun0",
    ip_mask: str = "",
    tun_type: str = "tun",
    up: bool = True,
):
    tun_fd = create_tun(tun_name, tun_type)

    if up:
        set_nic_up(tun_name)

    if ip_mask is not None and ip_mask != "":
        ip_mask = ip_mask.split("/")
        if len(ip_mask) == 1:
            ip = ip_mask
            mask_num = 24
        else:
            ip, mask_num = ip_mask
            mask_num = int(mask_num)
        mask = socket.inet_ntoa(struct.pack("I", 2**mask_num - 1))

        set_nic_ip_address(tun_name, ip)
        set_nic_netmask(tun_name, mask)

    # set_nic_ip_address(tun_name, "192.168.1.2", peer=True)
    # add_route('192.168.1.0', '192.168.1.2', '255.255.255.0')

    return tun_fd


class OriginPacketReader(AbstractReader):
    def __init__(self, reader: AbstractReader):
        self.reader = reader

    async def read(self, n: int) -> bytes:
        data = await self.reader.read(n)
        assert len(data) + 4 <= n
        if len(data) == 0:
            return data
        return struct.pack("I", len(data)) + data


class OriginPacketWriter(AbstractWriter):
    def __init__(self, writer: AbstractWriter):
        self.writer = writer
        self.buffer = b""

    async def write(self, data: bytes):
        self.buffer += data
        if len(self.buffer) >= DEFAULT_BUFFER_SIZE:
            await self.drain()

    async def drain(self):
        buffer = self.buffer
        off = 0
        while True:
            t_off = off
            if t_off + 4 > len(buffer):
                break
            pack_size_bytes = buffer[t_off : t_off + 4]
            t_off += 4
            (pack_size,) = struct.unpack("I", pack_size_bytes)
            if t_off + pack_size > len(buffer):
                break
            pack_bytes = buffer[t_off : t_off + pack_size]
            t_off += pack_size
            await self.writer.write(pack_bytes)
            off = t_off
        self.buffer = buffer[off:]

    async def write_eof(self):
        await self.writer.write_eof()

    async def close(self):
        await self.writer.close()


class TunPipe(Pipe):
    def __init__(
        self,
        tun_name: str = "tun0",
        ip_mask: str = "",
        tun_type: str = "tun",
        up: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.tun_name = tun_name
        self.ip_mask = ip_mask
        self.tun_type = tun_type
        self.up = up
        self.cancel_delay = 0.2

    async def open(self, callback: PipeCallback):
        tun_fd = init_tun(
            tun_name=self.tun_name,
            ip_mask=self.ip_mask,
            tun_type=self.tun_type,
            up=self.up,
        )
        tun_file = os.fdopen(tun_fd)
        try:
            reader = OriginPacketReader(await file_to_reader(tun_file))
            writer = OriginPacketWriter(await file_to_writer(tun_file))
            await callback(reader, writer)
        finally:
            tun_file.close()


def error_response(message: str):
    logging.debug(message)
    return aiohttp.web.Response(status=500, body=message.encode())


class HttpStreamParser:
    def __init__(self, request: aiohttp.web.Request):
        self.request = request
        self.is_websocket = False
        self.task = None
        self.reader: AbstractReader = None
        self.writer: AbstractWriter = None
        self.response: aiohttp.web.StreamResponse = None

    async def prepare(self):
        self.is_websocket = (
            self.request.headers.get("Connection", "").lower() == "upgrade"
            and self.request.headers.get("Upgrade", "").lower() == "websocket"
        )

        if not self.is_websocket:
            if (
                self.request.method != "GET"
                and self.request.headers.get("Transfer-Encoding", "").lower()
                != "chunked"
            ):
                raise Exception("Transfer-Encoding must be chunked")

        if self.is_websocket:
            self.response = aiohttp.web.WebSocketResponse()
            await self.response.prepare(self.request)
            self.task, self.reader, self.writer = get_websocket_streams(self.response)
        else:
            self.response = aiohttp.web.StreamResponse(headers={}, status=200)
            writer = await self.response.prepare(self.request)
            reader = self.request.content
            self.reader = AIOHttpStreamReader(reader)
            self.writer = AIOHttpStreamWriter(writer)

            async def run_check():
                try:
                    while True:
                        await asyncio.sleep(0.1)
                        await reader.read(0)
                except Exception:
                    ...

            self.task = asyncio.create_task(run_check())

    async def prepare_read(self):
        if not self.is_websocket:
            await cancel_and_wait(self.task)

    async def close(self):
        # await self.writer.close()
        if self.is_websocket:
            await self.response.close()
        await cancel_and_wait(self.task)


class DirectPipe(Pipe):
    def __init__(self, reader: AbstractReader, writer: AbstractWriter, **kwargs):
        super().__init__(**kwargs)
        self.reader = reader
        self.writer = writer

    async def open(self, callback: PipeCallback):
        await callback(self.reader, self.writer)


piping_info_map = {}


async def http_pipe_handler(request: aiohttp.web.Request):
    try:
        name = request.match_info.get("name", "")
        assert name != "", "Pipe name is empty"

        if name not in piping_info_map:
            is_first = True
            info = piping_info_map[name] = {
                "first_writer": None,
                "second_writer": None,
                "wait_peer_event": asyncio.Event(),
            }
        else:
            is_first = False
            info = piping_info_map[name]
            assert not info["wait_peer_event"].is_set(), "The pipe is already connected"

        parser = HttpStreamParser(request)
        await parser.prepare()

        pipe_proto = "ws" if parser.is_websocket is not None else "chunked"
        pipe_role = "first" if is_first else "second"
        pipe_name = f"{name}[{pipe_role},{pipe_proto}]"
        logging.debug("Http relay pipe %s was connected", pipe_name)

        timeout = int(request.query.get("timeout", "60"))

        wait_peer_event: asyncio.Event = info["wait_peer_event"]
        wait_peer_task = None

        try:
            if is_first:
                info["first_writer"] = parser.writer

                wait_peer_task = asyncio.create_task(wait_peer_event.wait())
                await asyncio.wait(
                    [wait_peer_task, parser.task],
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=timeout,
                )

                if not wait_peer_event.is_set():
                    return error_response(
                        f"Http relay pipe {pipe_name} disconnect before processing"
                    )

                peer_writer = info["second_writer"]
            else:
                info["second_writer"] = parser.writer
                wait_peer_event.set()
                peer_writer = info["first_writer"]

            await parser.prepare_read()

            peer_writer: AbstractWriter
            listener = ConnectPipeTransferListener(pipe_name)
            await transfer_stream_with_listener(parser.reader, peer_writer, listener)

            return parser.response
        finally:
            if name in piping_info_map:
                del piping_info_map[name]
            await parser.close()
            await cancel_and_wait(wait_peer_task)
            logging.debug("Http relay pipe %s exit", pipe_name)

    except Exception as e:
        logging.debug(e, exc_info=True)
        return error_response(str(e))


UNIX_SOCKET_DIR = os.environ.get("UNIX_SOCKET_DIR", "/tmp")
PUSH_RULES_HEADER_NAME = "Push-Rules"


def base64_encode(data):
    if type(data) == str:
        data = data.encode()
    data_bytes_base64 = base64.encodebytes(data)
    data_str_base64 = data_bytes_base64.decode()
    data_str_base64 = data_str_base64.replace("\n", "")
    return data_str_base64


http_rules_map = {}


async def http_push_handler(request: aiohttp.web.Request):
    try:
        name = request.match_info.get("name", "")
        assert name != "", "Pipe name is empty"

        http_rules = request.headers.get(PUSH_RULES_HEADER_NAME, "")
        if http_rules == "":
            http_rules = "path=path[len(PIPE_PREFIX):]"
        else:
            http_rules = base64.decodebytes(http_rules.encode()).decode()

        http_rules_map[name] = compile(http_rules, "", "exec")

        parser = HttpStreamParser(request)
        await parser.prepare()

        pipe_proto = "ws" if parser.is_websocket is not None else "chunked"
        pipe_name = f"{name}[{pipe_proto}]"
        logging.debug("Http push pipe %s was connected", pipe_name)

        try:
            await parser.prepare_read()

            pipe1 = DirectPipe(parser.reader, parser.writer, split=True)
            pipe2 = UnixServerPipe(f"{UNIX_SOCKET_DIR}/{name}.sock", fork=True)
            await connect_pipes(pipe1, pipe2)

            return parser.response
        finally:
            del http_rules_map[name]
            await parser.close()
            logging.debug("Http push pipe %s exit", pipe_name)

    except Exception as e:
        logging.debug(e, exc_info=True)
        return error_response(str(e))


def process_http_rules(
    url: str, headers: dict, exp: "str|types.CodeType", extra_env: dict
):
    url_obj = yarl.URL(url)
    before_info = {
        "url": url,
        "headers": dict(headers),
        "scheme": url_obj.scheme,
        "host_port": url_obj.authority,
        "host": url_obj.host,
        "port": url_obj.port,
        "path": url_obj.path,
        "query": dict(url_obj.query),
        "fragment": url_obj.fragment,
    }

    after_info = before_info.copy()
    for k in after_info:
        if isinstance(after_info[k], dict):
            after_info[k] = after_info[k].copy()

    exec(exp, extra_env, after_info)

    changed_keys = []
    for k in before_info:
        if before_info[k] != after_info[k]:
            changed_keys.append(k)

    if len(changed_keys) > 0:
        if "headers" in changed_keys:
            headers = after_info["headers"]
        if "url" in changed_keys:
            url = after_info["url"]
        else:
            args_dict = dict(
                scheme=after_info["scheme"],
                path=after_info["path"],
                query=after_info["query"],
                fragment=after_info["fragment"],
            )
            if "host_port" in changed_keys:
                args_dict.update(authority=after_info["host_port"])
            else:
                args_dict.update(host=after_info["host"], port=after_info["port"])
            url = str(yarl.URL.build(**args_dict))

    return url, headers


async def http_access_handler(request: aiohttp.web.Request):
    name = request.match_info.get("name", "")
    assert name != "", "Pipe name is empty"

    access_path_prefix = request.app["access_path_prefix"]
    pipe_prefix = access_path_prefix + name

    http_rules = http_rules_map[name]

    # request_chunked = request.headers.get("Transfer-Encoding", "") == "chunked"

    client_peer_reader, client_writer = create_directly_connected_streams()
    request_data = StreamReaderPayload(client_peer_reader)

    server_reader = request.content
    server_reader = AIOHttpStreamReader(server_reader)

    listener1 = ConnectPipeTransferListener(f"request[{name}]")
    request_transfer_task = asyncio.create_task(
        transfer_stream_with_listener(server_reader, client_writer, listener1)
    )

    extra_env = dict(PIPE_PREFIX=pipe_prefix)
    url, headers = process_http_rules(
        request.url, request.headers, http_rules, extra_env=extra_env
    )

    try:
        connector = aiohttp.UnixConnector(f"{UNIX_SOCKET_DIR}/{name}.sock")
        async with aiohttp.ClientSession(connector=connector) as http:
            async with http.request(
                request.method,
                url,
                headers=headers,
                timeout=None,
                data=request_data,
            ) as client_response:
                client_reader = AIOHttpStreamReader(client_response.content)

                server_response = aiohttp.web.StreamResponse(
                    headers=client_response.headers, status=client_response.status
                )
                server_writer = await server_response.prepare(request)
                server_writer = AIOHttpStreamWriter(server_writer)

                listener2 = ConnectPipeTransferListener(f"response[{name}]")
                await transfer_stream_with_listener(
                    client_reader, server_writer, listener2
                )
                await request_transfer_task

                return server_response
    finally:
        ...


class HttpRelayServer(Server):
    def __init__(
        self,
        bind_host: str = "0.0.0.0",
        bind_port: int = None,
        path_prefix: str = "",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.path_prefix = path_prefix

    async def run(self):
        app = aiohttp.web.Application()
        deploy_relay_server_to_app(app, self.path_prefix)
        await run_aiohttp_app(self.bind_host, self.bind_port, app)


def deploy_relay_server_to_app(app: aiohttp.web.Application, path_prefix):
    HTTP_PIPE_PREFIX = os.environ.get("HTTP_PIPE_PREFIX", "/pipe")
    HTTP_PUSH_PREFIX = os.environ.get("HTTP_PUSH_PREFIX", "/push")
    HTTP_ACCESS_PREFIX = os.environ.get("HTTP_ACCESS_PREFIX", "/access")

    path_handlers = [
        (path_prefix + HTTP_PIPE_PREFIX + "/{name:[^/]+}", http_pipe_handler),
        (path_prefix + HTTP_PUSH_PREFIX + "/{name:[^/]+}", http_push_handler),
        (
            path_prefix + HTTP_ACCESS_PREFIX + "/{name:[^/]+}{path:.*}",
            http_access_handler,
        ),
    ]

    for path, handler in path_handlers:
        app.router.add_route("*", path, handler)

    app.update(access_path_prefix=path_prefix + "/access/")


def split_outside_quotes(text, ch):
    def indexof_ignore_escape(text, ch, from_index, to_index):
        while from_index <= to_index:
            c = text[from_index]
            if c == "\\":
                from_index += 1
            else:
                if c == ch:
                    return from_index
            from_index += 1
        return -1

    def indexof_outside_quotes(text, ch, from_index, to_index):
        while from_index <= to_index:
            c = text[from_index]
            if c in ("'", '"'):
                from_index = indexof_ignore_escape(text, c, from_index + 1, to_index)
                if from_index == -1:
                    break
                c = text[from_index]
            if c == ch:
                return from_index
            from_index += 1
        return -1

    def split_outside_quotes(text, ch, from_index, to_index):
        results = []
        while True:
            i = indexof_outside_quotes(text, ch, from_index, to_index)
            if i == -1:
                results.append(text[from_index:])
                break
            results.append(text[from_index:i])
            from_index = i + 1
        new_results = []
        for result in results:
            result: str
            result = result.strip()
            if len(result) == 0:
                continue
            if result[0] in ("'", '"'):
                result = result[1:-1].encode("UTF-8").decode('unicode_escape')
            new_results.append(result)
        return new_results

    return split_outside_quotes(text, ch, 0, len(text) - 1)


def create_pipe(exp: str):
    exp_args = split_outside_quotes(exp, ",")
    main_args = split_outside_quotes(exp_args[0], ":")
    pipe_type = main_args[0].lower()
    args = {}
    for extra_arg in exp_args[1:]:
        arg_kv = split_outside_quotes(extra_arg, "=")
        key = arg_kv[0].replace("-", "_")
        if len(arg_kv) == 2:
            args[key] = arg_kv[1]
        elif len(arg_kv) == 1:
            args[key] = True
        else:
            raise
    if pipe_type == "-":
        pipe_cls = StdIOPipe
    elif pipe_type in ("tcp-l", "udp-l"):
        if pipe_type == "tcp-l":
            pipe_cls = TCPServerPipe
        elif pipe_type == "udp-l":
            pipe_cls = UDPServerPipe
        if len(main_args) == 2:
            args["bind_port"] = main_args[1]
        elif len(main_args) == 3:
            args["bind_host"] = main_args[1]
            args["bind_port"] = main_args[2]
        else:
            raise
    elif pipe_type in ("tcp", "udp"):
        if pipe_type == "tcp":
            pipe_cls = TCPPipe
        elif pipe_type == "udp":
            pipe_cls = UDPPipe
        if len(main_args) == 3:
            args["connect_host"] = main_args[1]
            args["connect_port"] = main_args[2]
        else:
            raise
    elif pipe_type in ("unix", "unix-l"):
        if pipe_type == "unix":
            pipe_cls = UnixPipe
        elif pipe_type == "unix-l":
            pipe_cls = UnixServerPipe
        if len(main_args) == 2:
            args["path"] = main_args[1]
        else:
            raise
    elif pipe_type in ("exec", "system"):
        pipe_cls = ExecPipe
        if len(main_args) == 2:
            args["cmd"] = main_args[1]
        else:
            raise
        if pipe_type == "system":
            args["shell"] = True
    elif pipe_type in ("http", "https"):
        pipe_cls = HttpPipe
        args["url"] = exp_args[0]
    elif pipe_type in ("ws", "wss"):
        pipe_cls = WebsocketPipe
        args["url"] = exp_args[0]
    elif pipe_type in ("http-l", "ws-l", "http-relay"):
        if pipe_type == "http-l":
            pipe_cls = HttpServerPipe
        elif pipe_type == "ws-l":
            pipe_cls = WebsocketServerPipe
        elif pipe_type == "http-relay":
            pipe_cls = HttpRelayServer
        if len(main_args) == 2:
            args["bind_port"] = main_args[1]
        elif len(main_args) == 3:
            args["bind_host"] = main_args[1]
            args["bind_port"] = main_args[2]
        else:
            raise
    elif pipe_type == "tun":
        pipe_cls = TunPipe
        if len(main_args) == 2:
            args["ip_mask"] = main_args[1]
        else:
            raise
    else:
        raise

    tmp_cls = pipe_cls
    while True:
        sig = inspect.signature(tmp_cls.__init__)
        for arg_name in args:
            if arg_name in sig.parameters:
                arg_type = sig.parameters[arg_name].annotation
                arg_value = args[arg_name]
                if arg_type == bool:
                    arg_value = bool(int(arg_value))
                else:
                    arg_value = arg_type(arg_value)
                args[arg_name] = arg_value
        tmp_cls = tmp_cls.__base__
        if tmp_cls == object:
            break

    try:
        return pipe_cls(**args)
    except TypeError as e:
        logging.error("Class %s construct error", pipe_cls)
        raise


async def async_app_main(exps: str, verbose=True):
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(stream=sys.stderr, level=log_level)

    if len(exps) not in (1, 2):
        logging.error("Exp count must be 1 or 2")
        return

    targets = [create_pipe(exp) for exp in exps]

    target_type = Pipe if len(exps) == 2 else Server
    for target in targets:
        if not isinstance(target, target_type):
            logging.error("%s is not instance of %s", target, target_type.__name__)
            return

    if len(exps) == 2:
        await connect_pipes(*targets)
    else:
        await targets[0].run()


def app_main(*args, **kwargs):
    loop = asyncio.new_event_loop()

    def shutdown(sig: signal.Signals) -> None:
        for task in asyncio.all_tasks(loop):
            task.cancel()

    if platform.system() != "Windows":
        for sig in [signal.SIGINT, signal.SIGTERM]:
            loop.add_signal_handler(sig, lambda: shutdown(sig))

    try:
        return loop.run_until_complete(async_app_main(*args, **kwargs))
    except asyncio.CancelledError:
        ...
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.stop()
        loop.close()


def main():
    DEBUG_ARGS = os.environ.get("DEBUG_ARGS", "")

    if DEBUG_ARGS != "" and len(sys.argv) == 1:
        sys.argv += split_outside_quotes(DEBUG_ARGS, " ")
        sys.argv += ["-v"]

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "exps",
        type=str,
        nargs="*",
        help="Pipe expressions",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose log",
    )
    app_main(**vars(parser.parse_args()))


if __name__ == "__main__":
    ...
    # export DEBUG_ARGS=""
    # export DEBUG_ARGS="tcp-l:1111,reuseaddr -"
    # export DEBUG_ARGS="tcp:127.0.0.1:1111 -"
    # export DEBUG_ARGS="tcp:127.0.0.1:9080 tcp-l:2222,reuseaddr"
    # export DEBUG_ARGS="system:'ls -lh /proc/self/fd',fdin=29,fdout=30 -"
    # export DEBUG_ARGS="system:'bash' -"
    # export DEBUG_ARGS="system:'bash <&29 >&30',fdin=29,fdout=30 -"
    # export DEBUG_ARGS="http-relay:1111"
    # export DEBUG_ARGS="http://127.0.0.1:1111/pipe/aaa -"
    # export DEBUG_ARGS="ws://127.0.0.1:1111/pipe/aaa -"
    # export DEBUG_ARGS="http://127.0.0.1:1111/pipe/aaa tcp:127.0.0.1:9080"
    # export DEBUG_ARGS="http://127.0.0.1:1111/pipe/aaa tcp-l:2222,reuseaddr"
    # export DEBUG_ARGS="http://127.0.0.1:1111/pipe/aaa,split tcp:127.0.0.1:9080"
    # export DEBUG_ARGS="http://127.0.0.1:1111/pipe/aaa,split tcp-l:2222,reuseaddr,fork"
    # export DEBUG_ARGS="tcp-l:1111,reuseaddr,split tcp:127.0.0.1:9080"
    # export DEBUG_ARGS="tcp:127.0.0.1:1111,split tcp-l:2222,reuseaddr,fork"
    # export DEBUG_ARGS="tcp:127.0.0.1:9080 http://127.0.0.1:1111/push/xxx,split,push_rules="'"'"path=path[len(PIPE_PREFIX):]"'"'
    # export DEBUG_ARGS="http://127.0.0.1:1111/pipe/aaa tun:192.168.7.2/24"

    main()
