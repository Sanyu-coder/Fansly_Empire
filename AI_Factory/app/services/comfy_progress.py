import os
import json
import time
import uuid
import socket
import struct
import base64
import threading
from urllib.parse import urlparse
import requests

PROGRESS_PREFIX = "@@PROGRESS@@ "


def emit_progress(pct=None, stage=None, completed=None, total=None):
    """
    サブプロセスから worker_service.py へ構造化進捗シグナルを即時送信する。
    """
    payload = {}
    if pct is not None:
        payload["pct"] = max(0, min(100, int(round(pct))))
    if stage is not None:
        payload["stage"] = str(stage)
    if completed is not None:
        payload["completed"] = int(completed)
    if total is not None:
        payload["total"] = int(total)
    try:
        line = f"\n{PROGRESS_PREFIX}{json.dumps(payload, ensure_ascii=False)}"
        print(line, flush=True)
    except Exception:
        pass


class _SimpleWsClient:
    """
    外部パッケージ不要の、Python標準ライブラリのみで動作する
    ComfyUI /ws 受信専用の軽量 WebSocket クライアント (RFC 6455)。
    """

    def __init__(self, ws_url: str, on_message):
        self.ws_url = ws_url
        self.on_message = on_message
        self.sock = None
        self.running = False
        self.connected_event = threading.Event()
        self._thread = None

    def start(self, connect_timeout: float = 2.0):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.connected_event.wait(timeout=connect_timeout)

    def close(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _send_pong(self, payload: bytes = b""):
        try:
            mask_key = os.urandom(4)
            length = len(payload)
            header = bytearray([0x8A])
            if length < 126:
                header.append(0x80 | length)
            elif length < 65536:
                header.append(0x80 | 126)
                header.extend(struct.pack("!H", length))
            else:
                return
            masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            self.sock.sendall(bytes(header) + mask_key + masked)
        except Exception:
            pass

    def _run(self):
        try:
            parsed = urlparse(self.ws_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 8188
            path = parsed.path or "/ws"
            if parsed.query:
                path = f"{path}?{parsed.query}"

            self.sock = socket.create_connection((host, port), timeout=5.0)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            handshake = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            self.sock.sendall(handshake.encode("ascii"))

            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = self.sock.recv(1024)
                if not chunk:
                    return
                resp += chunk
            header_part, leftover = resp.split(b"\r\n\r\n", 1)
            if b"101" not in header_part.split(b"\r\n")[0]:
                return

            self.connected_event.set()
            self.sock.settimeout(2.0)
            buffer = bytearray(leftover)

            def read_bytes(n: int) -> bytes:
                while len(buffer) < n and self.running:
                    try:
                        chunk = self.sock.recv(max(4096, n - len(buffer)))
                        if not chunk:
                            raise ConnectionError("EOF")
                        buffer.extend(chunk)
                    except socket.timeout:
                        continue
                out = bytes(buffer[:n])
                del buffer[:n]
                return out

            while self.running:
                b1, b2 = read_bytes(2)
                opcode = b1 & 0x0F
                masked = (b2 & 0x80) != 0
                payload_len = b2 & 0x7F

                if payload_len == 126:
                    payload_len = struct.unpack("!H", read_bytes(2))[0]
                elif payload_len == 127:
                    payload_len = struct.unpack("!Q", read_bytes(8))[0]

                mask_key = read_bytes(4) if masked else None
                payload = read_bytes(payload_len) if payload_len > 0 else b""
                if masked and mask_key:
                    payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

                if opcode == 0x8:
                    break
                elif opcode == 0x9:
                    self._send_pong(payload)
                elif opcode == 0x1:
                    try:
                        text = payload.decode("utf-8", errors="replace")
                        msg = json.loads(text)
                        if self.on_message:
                            self.on_message(msg)
                    except Exception:
                        pass
        except Exception:
            self.connected_event.set()
        finally:
            self.close()


def wait_comfy_prompt_with_progress(
    comfy_url: str,
    workflow_dict: dict,
    on_node=None,
    on_step=None,
    poll_interval: float = 1.0,
) -> dict:
    """
    ComfyUI にワークフローを投入し、WebSocketでリアルタイムにノード遷移・ステップ進捗を受け取りながら
    完了時の outputs 辞書を返す。
    """
    client_id = str(uuid.uuid4())
    ws_url = comfy_url.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?clientId={client_id}"
    prompt_id_holder = {"id": None}

    def _handle_ws_msg(msg: dict):
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        data = msg.get("data") or {}
        pid = data.get("prompt_id")
        target_pid = prompt_id_holder["id"]
        if target_pid and pid and pid != target_pid:
            return

        if mtype == "executing":
            node_id = data.get("node")
            if node_id is not None and on_node:
                try:
                    on_node(str(node_id))
                except Exception:
                    pass
        elif mtype == "progress":
            val = data.get("value", 0)
            mx = data.get("max", 0)
            node_id = str(data.get("node") or "")
            if mx and mx > 0 and on_step:
                try:
                    on_step(int(val), int(mx), node_id)
                except Exception:
                    pass

    ws_client = _SimpleWsClient(ws_url, _handle_ws_msg)
    ws_client.start(connect_timeout=1.5)

    try:
        res = requests.post(
            f"{comfy_url}/prompt",
            json={"prompt": workflow_dict, "client_id": client_id},
            timeout=30,
        ).json()
        prompt_id = res["prompt_id"]
        prompt_id_holder["id"] = prompt_id

        while True:
            history = requests.get(f"{comfy_url}/history/{prompt_id}", timeout=30).json()
            if prompt_id in history:
                return history[prompt_id].get("outputs", {})
            time.sleep(poll_interval)
    finally:
        ws_client.close()
