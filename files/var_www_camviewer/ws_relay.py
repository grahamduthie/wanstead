#!/usr/bin/env python3
"""WansteadCam WebSocket MJPEG relay.

Reads the MJPEG stream from ustreamer (localhost:8080) and pushes
individual JPEG frames to connected WebSocket clients.

Designed for Safari/iOS which doesn't handle multipart/x-mixed-replace
reliably. Clients connect via ws:// and receive binary JPEG frames.

Runs under systemd: wcam-ws-relay.service
"""
import asyncio
import logging
import logging.handlers
import sys

import websockets

# --- Configuration ---
USTREAMER_HOST = "127.0.0.1"
USTREAMER_PORT = 8080
USTREAMER_PATH = "/stream"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8087
BOUNDARY = b"--boundarydonotcross"
MAX_CLIENTS = 10

# --- Safe logging handler: falls back to stderr on any I/O error ---

class SafeRotatingHandler(logging.handlers.BaseRotatingHandler):
    """RotatingFileHandler that never crashes. Falls back to stderr on I/O errors."""

    def __init__(self, filename, maxBytes=0, backupCount=0, encoding='utf-8'):
        self._filename = filename
        self._maxBytes = maxBytes
        self._backupCount = backupCount
        self._encoding = encoding
        self._stream = None
        self._broken = False
        logging.Handler.__init__(self)
        self.baseFilename = filename
        self.maxBytes = maxBytes
        self.backupCount = backupCount
        self._open_file()

    def _open_file(self):
        try:
            if self._stream and not self._stream.closed:
                self._stream.close()
            self._stream = open(self.baseFilename, 'a', encoding=self._encoding)
            self._broken = False
        except OSError:
            self._broken = True
            self._stream = None
            print(f'SAFE_HANDLER_BROKEN: cannot open {self._filename}', file=sys.stderr)

    def shouldRollover(self, record):
        if self._broken or self._stream is None:
            return False
        self._stream.seek(0, 2)
        if self._stream.tell() + len(self.format(record)) >= self._maxBytes:
            return 1
        return 0

    def doRollover(self):
        if self.backupCount > 0:
            for i in range(self.backupCount - 1, 0, -1):
                sfn = f'{self.baseFilename}.{i}'
                dfn = f'{self.baseFilename}.{i + 1}'
                if os.path.exists(sfn):
                    if os.path.exists(dfn):
                        os.remove(dfn)
                    os.rename(sfn, dfn)
            dfn = self.baseFilename + '.1'
            if os.path.exists(self.baseFilename):
                os.rename(self.baseFilename, dfn)
        self._open_file()

    def emit(self, record):
        try:
            if self._broken:
                self._open_file()
                if self._broken:
                    raise OSError(f'Cannot open {self._filename}')
            if self.shouldRollover(record):
                self.doRollover()
                if self._broken:
                    raise OSError(f'Rollover failed for {self._filename}')
            if self._stream is None:
                raise OSError(f'No stream for {self._filename}')
            msg = self.format(record)
            self._stream.write(msg + self.terminator)
            self._stream.flush()
        except Exception:
            self._broken = True
            if self._stream and not self._stream.closed:
                self._stream.close()
            self._stream = None
            print(f'LOG_FALLBACK: {self.format(record)}', file=sys.stderr)


# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        SafeRotatingHandler(
            "/var/log/wcam-ws-relay.log",
            maxBytes=500_000,
            backupCount=3,
        ),
    ],
)
logger = logging.getLogger("wcam-ws-relay")

# --- Connected clients ---
clients = set()


async def fetch_mjpeg_stream():
    """Continuously read the MJPEG stream from ustreamer and broadcast frames.

    Throttles to ~20fps (50ms minimum between frames) to prevent Safari
    clients from being overwhelmed. At 30fps the JPEG decode queue builds
    up and causes periodic GC pauses that manifest as jerky playback.
    """
    MIN_FRAME_INTERVAL = 0.05  # 50ms = max ~20fps to clients
    last_broadcast = 0.0

    while True:
        reader = None
        try:
            reader, writer = await asyncio.open_connection(
                USTREAMER_HOST, USTREAMER_PORT
            )
            request = (
                f"GET {USTREAMER_PATH} HTTP/1.1\r\n"
                f"Host: {USTREAMER_HOST}:{USTREAMER_PORT}\r\n"
                f"Connection: keep-alive\r\n"
                f"\r\n"
            )
            writer.write(request.encode())
            await writer.drain()

            # Read HTTP response headers
            headers = b""
            while True:
                line = await reader.readline()
                if line == b"\r\n":
                    break
                headers += line
                if not line:
                    break

            logger.info("Connected to ustreamer MJPEG stream")

            buffer = b""
            while True:
                chunk = await reader.read(8192)
                if not chunk:
                    logger.warning("Stream ended, reconnecting...")
                    break
                buffer += chunk

                # Split on boundary
                while BOUNDARY in buffer:
                    before, buffer = buffer.split(BOUNDARY, 1)

                    # Extract JPEG from the part before the next boundary
                    if b"\r\n\r\n" in before:
                        _, jpeg_data = before.split(b"\r\n\r\n", 1)
                        # Find the JPEG end marker (FF D9)
                        eoi_pos = jpeg_data.find(b"\xff\xd9")
                        if eoi_pos != -1:
                            jpeg_data = jpeg_data[: eoi_pos + 2]
                            if jpeg_data and clients:
                                # Throttle: skip frames that arrive too soon
                                now = asyncio.get_event_loop().time()
                                elapsed = now - last_broadcast
                                if elapsed < MIN_FRAME_INTERVAL:
                                    continue  # skip this frame
                                last_broadcast = now

                                # Broadcast to all connected clients
                                dead = set()
                                for ws in list(clients):
                                    try:
                                        await ws.send(jpeg_data)
                                    except Exception:
                                        dead.add(ws)
                                clients.difference_update(dead)

        except Exception as e:
            logger.warning("MJPEG stream disconnected: %s", e)
        finally:
            if reader:
                try:
                    pass
                except Exception:
                    pass
        # Reconnect after a short delay
        await asyncio.sleep(1)


async def handle_client(websocket):
    """Handle a single WebSocket client connection."""
    clients.add(websocket)
    logger.info("Client connected (%d total)", len(clients))
    try:
        await websocket.wait_closed()
    finally:
        clients.discard(websocket)
        logger.info("Client disconnected (%d total)", len(clients))


async def main():
    logger.info("Starting WebSocket relay on %s:%d", LISTEN_HOST, LISTEN_PORT)
    # Start the MJPEG fetcher as a background task
    asyncio.create_task(fetch_mjpeg_stream())

    async with websockets.serve(
        handle_client,
        LISTEN_HOST,
        LISTEN_PORT,
        max_size=10 * 1024 * 1024,  # 10MB max frame size
    ):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
