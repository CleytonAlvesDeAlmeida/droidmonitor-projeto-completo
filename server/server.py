#!/usr/bin/env python3
"""
DroidMonitor - server.py
Estende a tela de um PC Linux para um celular Android via WebRTC, na rede
local, sem nuvem e sem login. Autenticação por PIN de 6 dígitos exibido
localmente. Controle remoto (mouse/teclado) opcional via checkbox.

Fluxo:
  1. Ao iniciar, abre uma janela pequena (Tkinter) mostrando o PIN gerado
     e um checkbox "Permitir controle".
  2. Anuncia o serviço na rede local via mDNS (_droidmonitor._tcp.local.).
  3. Sobe um servidor de sinalização WebSocket (aiohttp) na porta 8765,
     que só aceita conexões de IPs de rede local (192.168.x.x, 10.x.x.x,
     172.16-31.x.x) ou localhost (necessário para o modo USB/ADB forward).
  4. O celular conecta, envia o PIN; se correto, troca SDP para estabelecer
     WebRTC (DTLS/SRTP obrigatórios, como em qualquer conexão WebRTC).
  5. O vídeo da tela é capturado com mss e enviado como VideoStreamTrack.
  6. Eventos de toque chegam por um DataChannel e, se o checkbox
     "Permitir controle" estiver marcado, são convertidos em cliques/
     movimentos de mouse via pyautogui.

Uso:
    python3 server.py

Requisitos: ver requirements.txt
"""

import asyncio
import concurrent.futures
import fractions
import ipaddress
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import mss
import numpy as np
import pyautogui
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaRelay
from av import VideoFrame
from zeroconf import ServiceInfo, Zeroconf
import socket

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

APP_NAME = "DroidMonitor"
PORT = 8765
SERVICE_TYPE = "_droidmonitor._tcp.local."

# Presets de qualidade: (largura, altura, fps, jpeg/bitrate hint)
QUALITY_PRESETS = {
    "baixa":  {"width": 640,  "height": 360,  "fps": 15},
    "media":  {"width": 1280, "height": 720,  "fps": 24},
    "alta":   {"width": 1920, "height": 1080, "fps": 30},
}
DEFAULT_QUALITY = "media"

MAX_PIN_ATTEMPTS = 5          # tentativas de PIN erradas antes de bloquear IP
PIN_BLOCK_SECONDS = 60        # bloqueio temporário após exceder tentativas
PIN_LENGTH = 6

pyautogui.FAILSAFE = False    # evita abortar por causa do canto da tela
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------
# Estado global compartilhado (PIN, permissão de controle, etc.)
# --------------------------------------------------------------------------

@dataclass
class AppState:
    pin: str = field(default_factory=lambda: "".join(secrets.choice("0123456789") for _ in range(PIN_LENGTH)))
    allow_control: bool = False
    quality: str = DEFAULT_QUALITY
    failed_attempts: dict = field(default_factory=dict)   # ip -> (count, blocked_until)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def register_failed_attempt(self, ip: str) -> bool:
        """Retorna True se o IP acabou de ser bloqueado."""
        with self.lock:
            count, blocked_until = self.failed_attempts.get(ip, (0, 0))
            count += 1
            newly_blocked = False
            if count >= MAX_PIN_ATTEMPTS:
                blocked_until = time.time() + PIN_BLOCK_SECONDS
                newly_blocked = True
            self.failed_attempts[ip] = (count, blocked_until)
            return newly_blocked

    def is_blocked(self, ip: str) -> bool:
        with self.lock:
            count, blocked_until = self.failed_attempts.get(ip, (0, 0))
            if blocked_until and time.time() < blocked_until:
                return True
            if blocked_until and time.time() >= blocked_until:
                self.failed_attempts[ip] = (0, 0)
            return False

    def clear_attempts(self, ip: str):
        with self.lock:
            self.failed_attempts.pop(ip, None)


STATE = AppState()
relay = MediaRelay()
pcs: set[RTCPeerConnection] = set()


# --------------------------------------------------------------------------
# Restrição de rede local (bloqueia qualquer coisa que não seja LAN/localhost)
# --------------------------------------------------------------------------

def is_local_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip.is_loopback:
        return True  # necessário para adb forward tcp:8765 tcp:8765 (USB)
    if isinstance(ip, ipaddress.IPv4Address):
        return (
            ip in ipaddress.ip_network("192.168.0.0/16")
            or ip in ipaddress.ip_network("10.0.0.0/8")
            or ip in ipaddress.ip_network("172.16.0.0/12")
        )
    return False


@web.middleware
async def local_network_only_middleware(request: web.Request, handler):
    peer_ip = request.remote
    if not peer_ip or not is_local_ip(peer_ip):
        log.warning("Conexão recusada de IP não-local: %s", peer_ip)
        raise web.HTTPForbidden(text="Somente rede local é permitida.")
    return await handler(request)


# --------------------------------------------------------------------------
# Captura de tela -> VideoStreamTrack
# --------------------------------------------------------------------------

class ScreenCaptureTrack(VideoStreamTrack):
    """Captura a tela com mss e entrega frames para o aiortc.

    A captura (mss.grab) e o redimensionamento (PyAV/libswscale) são
    bloqueantes e consomem CPU. Rodá-los direto na coroutine recv() travaria
    o loop de eventos do asyncio — o mesmo loop que o aiortc usa para
    manter a conexão WebRTC viva (respostas a checks de "consent freshness"
    do ICE, envio de RTP, etc.). Por isso essa parte roda numa thread
    dedicada via run_in_executor, mantendo recv() de fato assíncrona.
    """

    def __init__(self, quality: str = DEFAULT_QUALITY):
        super().__init__()
        # Uma única thread dedicada: o mss (principalmente no Windows) tem
        # afinidade de thread para seus recursos internos (GDI), então a
        # instância de mss.mss() precisa ser criada e usada sempre na
        # mesma thread — daí max_workers=1 e a criação lazy dentro dela.
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._sct = None  # criado dentro da thread dedicada, no primeiro uso
        self._monitor = None
        self.set_quality(quality)
        self._time_base = fractions.Fraction(1, 90000)
        self._frame_count = 0
        self._start_time = None

    def set_quality(self, quality: str):
        preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS[DEFAULT_QUALITY])
        self._target_w = preset["width"]
        self._target_h = preset["height"]
        self._fps = preset["fps"]
        self._frame_interval = 1.0 / self._fps

    def _capture_and_convert(self):
        """Roda inteiramente na thread dedicada: captura, converte e redimensiona."""
        if self._sct is None:
            self._sct = mss.mss()
            self._monitor = self._sct.monitors[1]  # monitor principal

        raw = self._sct.grab(self._monitor)
        img = np.array(raw)  # BGRA
        img = np.ascontiguousarray(img[:, :, :3])  # descarta alpha -> BGR contíguo

        frame = VideoFrame.from_ndarray(img, format="bgr24")
        if (frame.width, frame.height) != (self._target_w, self._target_h):
            frame = frame.reformat(width=self._target_w, height=self._target_h)
        return frame

    async def recv(self):
        if self._start_time is None:
            self._start_time = time.time()

        # Ritmo de captura de acordo com o fps do preset atual
        next_frame_time = self._start_time + self._frame_count * self._frame_interval
        now = time.time()
        if next_frame_time > now:
            await asyncio.sleep(next_frame_time - now)

        loop = asyncio.get_event_loop()
        frame = await loop.run_in_executor(self._executor, self._capture_and_convert)

        pts = int(self._frame_count * (90000 / self._fps))
        frame.pts = pts
        frame.time_base = self._time_base
        self._frame_count += 1
        return frame

    def close(self):
        """Encerra a thread dedicada de captura. Chamar quando a conexão terminar."""
        self._executor.shutdown(wait=False)


# --------------------------------------------------------------------------
# Controle remoto (DataChannel -> pyautogui)
# --------------------------------------------------------------------------

def handle_control_message(raw_msg: str, screen_track: ScreenCaptureTrack):
    """Processa eventos de toque/teclado vindos do Android.

    Formato esperado (JSON), coordenadas normalizadas 0.0-1.0:
      {"type": "tap", "x": 0.5, "y": 0.5}
      {"type": "move", "x": 0.5, "y": 0.5}
      {"type": "down", "x": 0.5, "y": 0.5}
      {"type": "up", "x": 0.5, "y": 0.5}
      {"type": "key", "key": "enter"}
      {"type": "quality", "value": "alta"}
    """
    if not STATE.allow_control:
        return  # checkbox "Permitir controle" desmarcado: ignora tudo

    try:
        msg = json.loads(raw_msg)
    except (json.JSONDecodeError, TypeError):
        return

    mtype = msg.get("type")
    screen_w, screen_h = pyautogui.size()

    if mtype in ("tap", "move", "down", "up"):
        x = float(msg.get("x", 0))
        y = float(msg.get("y", 0))
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        px, py = int(x * screen_w), int(y * screen_h)

        if mtype == "tap":
            pyautogui.click(px, py)
        elif mtype == "move":
            pyautogui.moveTo(px, py, _pause=False)
        elif mtype == "down":
            pyautogui.mouseDown(px, py)
        elif mtype == "up":
            pyautogui.mouseUp(px, py)

    elif mtype == "key":
        key = msg.get("key")
        if key:
            try:
                pyautogui.press(key)
            except Exception:
                log.debug("Tecla não reconhecida: %s", key)


# --------------------------------------------------------------------------
# Sinalização WebSocket
# --------------------------------------------------------------------------

async def websocket_handler(request: web.Request):
    # Sem heartbeat automático: o WebSocket de sinalização só precisa ficar
    # vivo para a troca inicial de PIN/SDP e para mudanças de qualidade
    # depois. Um heartbeat agressivo aqui fechava a sinalização sozinho após
    # ~20s (por falta de "pong" a tempo), derrubando o WebRTC junto mesmo
    # com a conexão de vídeo saudável.
    ws = web.WebSocketResponse(heartbeat=None)
    await ws.prepare(request)
    peer_ip = request.remote

    if STATE.is_blocked(peer_ip):
        await ws.send_json({"type": "error", "message": "IP temporariamente bloqueado por excesso de tentativas de PIN."})
        await ws.close()
        return ws

    pc: Optional[RTCPeerConnection] = None
    screen_track: Optional[ScreenCaptureTrack] = None
    authenticated = False

    log.info("Nova conexão de sinalização de %s", peer_ip)

    try:
        async for message in ws:
            if message.type != web.WSMsgType.TEXT:
                continue

            data = json.loads(message.data)
            mtype = data.get("type")

            # 1) Sem PIN correto, nada mais é aceito
            if mtype == "pin":
                submitted = str(data.get("pin", ""))
                if submitted == STATE.pin:
                    authenticated = True
                    STATE.clear_attempts(peer_ip)
                    await ws.send_json({"type": "pin_ok"})
                    log.info("PIN correto de %s", peer_ip)
                else:
                    newly_blocked = STATE.register_failed_attempt(peer_ip)
                    await ws.send_json({
                        "type": "pin_error",
                        "blocked": newly_blocked,
                    })
                    log.warning("PIN incorreto de %s", peer_ip)
                    if newly_blocked:
                        await ws.close()
                        break
                continue

            if not authenticated:
                await ws.send_json({"type": "error", "message": "Envie o PIN primeiro."})
                continue

            # 2) Oferta SDP -> cria a RTCPeerConnection para essa sessão
            if mtype == "offer":
                quality = data.get("quality", STATE.quality)
                if quality not in QUALITY_PRESETS:
                    quality = DEFAULT_QUALITY
                STATE.quality = quality

                pc = RTCPeerConnection()
                pcs.add(pc)
                screen_track = ScreenCaptureTrack(quality=quality)
                pc.addTrack(relay.subscribe(screen_track))

                @pc.on("datachannel")
                def on_datachannel(channel):
                    log.info("DataChannel de controle aberto: %s", channel.label)

                    @channel.on("message")
                    def on_message(msg):
                        handle_control_message(msg, screen_track)

                @pc.on("connectionstatechange")
                async def on_state_change():
                    log.info("Estado da conexão WebRTC: %s", pc.connectionState)
                    # "disconnected" pode ser transitório (o ICE tenta se
                    # recuperar sozinho); só encerramos de fato em "failed"
                    # (falha definitiva) ou "closed" (já foi fechada).
                    if pc.connectionState in ("failed", "closed"):
                        pcs.discard(pc)
                        screen_track.close()
                        await pc.close()

                offer = RTCSessionDescription(sdp=data["sdp"], type=data["sdpType"])
                await pc.setRemoteDescription(offer)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)

                # Espera a coleta de candidatos ICE terminar antes de enviar a
                # answer, para garantir que a SDP já saia com todos os
                # candidatos embutidos (mesmo cuidado aplicado no Android).
                if pc.iceGatheringState != "complete":
                    gathering_done = asyncio.Event()

                    @pc.on("icegatheringstatechange")
                    def on_ice_gathering_change():
                        if pc.iceGatheringState == "complete":
                            gathering_done.set()

                    try:
                        await asyncio.wait_for(gathering_done.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        log.warning("Timeout esperando coleta de candidatos ICE; enviando SDP mesmo assim.")

                await ws.send_json({
                    "type": "answer",
                    "sdp": pc.localDescription.sdp,
                    "sdpType": pc.localDescription.type,
                })

            elif mtype == "ice_candidate":
                # Trickle ICE opcional; aiortc já inclui candidatos no SDP
                # por padrão, então normalmente isto não é necessário.
                pass

            elif mtype == "quality" and screen_track is not None:
                q = data.get("value")
                if q in QUALITY_PRESETS:
                    screen_track.set_quality(q)
                    STATE.quality = q
                    log.info("Qualidade alterada para: %s", q)

    except Exception as exc:
        log.exception("Erro na sessão de %s: %s", peer_ip, exc)
    finally:
        if screen_track is not None:
            screen_track.close()
        if pc is not None:
            pcs.discard(pc)
            await pc.close()
        log.info("Conexão encerrada de %s", peer_ip)

    return ws


async def status_handler(request: web.Request):
    """Endpoint simples de metadados, útil para depuração local."""
    return web.json_response({
        "name": APP_NAME,
        "allow_control": STATE.allow_control,
        "quality": STATE.quality,
    })


# --------------------------------------------------------------------------
# mDNS (descoberta na rede local)
# --------------------------------------------------------------------------

def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def start_mdns(hostname: str) -> Zeroconf:
    zeroconf = Zeroconf()
    local_ip = get_local_ip()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{hostname}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(local_ip)],
        port=PORT,
        properties={"name": hostname},
        server=f"{hostname}.local.",
    )
    zeroconf.register_service(info)
    log.info("Anunciado via mDNS como '%s' em %s:%d", hostname, local_ip, PORT)
    return zeroconf


# --------------------------------------------------------------------------
# Interface local: PIN + checkbox "Permitir controle"
# --------------------------------------------------------------------------

def start_ui():
    """Janela Tkinter simples rodando em thread separada.

    Mostra o PIN atual e um checkbox que liga/desliga STATE.allow_control.
    Roda em thread própria para não bloquear o loop assíncrono do servidor.
    """
    import tkinter as tk

    def run():
        root = tk.Tk()
        root.title(APP_NAME)
        root.geometry("320x220")
        root.resizable(False, False)

        tk.Label(root, text=APP_NAME, font=("Sans", 16, "bold")).pack(pady=(15, 5))
        tk.Label(root, text="PIN de conexão:", font=("Sans", 11)).pack()
        tk.Label(root, text=STATE.pin, font=("Sans", 28, "bold")).pack(pady=(0, 15))

        control_var = tk.BooleanVar(value=STATE.allow_control)

        def on_toggle():
            STATE.allow_control = control_var.get()
            log.info("Permitir controle: %s", STATE.allow_control)

        tk.Checkbutton(
            root,
            text="Permitir controle (mouse/teclado)",
            variable=control_var,
            command=on_toggle,
        ).pack(pady=5)

        tk.Label(root, text=f"Rede local, porta {PORT}", fg="gray").pack(pady=(10, 0))
        tk.Label(root, text="Sem PIN, ninguém conecta.", fg="gray").pack()

        root.mainloop()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------

async def on_shutdown(app: web.Application):
    for pc in list(pcs):
        await pc.close()
    pcs.clear()


def build_app() -> web.Application:
    app = web.Application(middlewares=[local_network_only_middleware])
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/status", status_handler)
    app.on_shutdown.append(on_shutdown)
    return app


def main():
    hostname = socket.gethostname().split(".")[0]

    print("=" * 50)
    print(f"  {APP_NAME}")
    print("=" * 50)
    print(f"  PIN de conexão: {STATE.pin}")
    print(f"  Porta: {PORT}")
    print("  Modo Wi-Fi: conecte pelo app Android na mesma rede local.")
    print(f"  Modo USB:   adb forward tcp:{PORT} tcp:{PORT}")
    print("=" * 50)

    try:
        start_ui()
    except Exception as exc:
        log.warning("Interface gráfica indisponível (%s); use somente o console.", exc)

    zeroconf = start_mdns(hostname)
    app = build_app()

    try:
        web.run_app(app, host="0.0.0.0", port=PORT, print=None)
    finally:
        zeroconf.close()


if __name__ == "__main__":
    main()
