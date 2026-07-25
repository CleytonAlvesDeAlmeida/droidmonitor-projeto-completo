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
import os
import queue
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

APP_NAME = "NuDuck"
PORT = 8765
SERVICE_TYPE = "_droidmonitor._tcp.local."

# Presets de qualidade por altura (a largura é calculada a partir da proporção
# real da tela do PC, então nunca estica nem corta a imagem). "auto" não é um
# preset fixo: a track ajusta sozinha entre os presets abaixo, subindo ou
# descendo conforme a carga de captura/codificação (ver ScreenCaptureTrack).
QUALITY_PRESETS = {
    "144p":  {"height": 144,  "fps": 15},
    "240p":  {"height": 240,  "fps": 15},
    "360p":  {"height": 360,  "fps": 20},
    "480p":  {"height": 480,  "fps": 24},
    "720p":  {"height": 720,  "fps": 30},
    "1080p": {"height": 1080, "fps": 30},
}
QUALITY_ORDER = ["144p", "240p", "360p", "480p", "720p", "1080p"]  # da menor pra maior
DEFAULT_QUALITY = "480p"


def is_valid_quality(value: str) -> bool:
    return value == "auto" or value in QUALITY_PRESETS

MAX_PIN_ATTEMPTS = 5          # tentativas de PIN erradas antes de bloquear IP
PIN_BLOCK_SECONDS = 60        # bloqueio temporário após exceder tentativas
PIN_LENGTH = 6

pyautogui.FAILSAFE = False    # evita abortar por causa do canto da tela
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(APP_NAME)

# Buffer de logs em memória, consumido pelo botão "Ver terminal" da janela.
# Guarda tudo (não só o nosso logger) pra também mostrar erros de
# aiohttp/aiortc — útil quando o processo roda sem console visível.
LOG_QUEUE: "queue.Queue[str]" = queue.Queue(maxsize=5000)


class _QueueLogHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_QUEUE.put_nowait(self.format(record))
        except queue.Full:
            pass


_queue_handler = _QueueLogHandler()
_queue_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(_queue_handler)


# --------------------------------------------------------------------------
# Estado global compartilhado (PIN, permissão de controle, etc.)
# --------------------------------------------------------------------------

@dataclass
class AppState:
    pin: str = field(default_factory=lambda: "".join(secrets.choice("0123456789") for _ in range(PIN_LENGTH)))
    allow_control: bool = False
    quality: str = DEFAULT_QUALITY
    usb_status: str = "checking"  # ver USB_STATUS_LABELS, atualizado pela thread de auto-forward
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

    A captura (mss.grab) e o redimensionamento são bloqueantes e consomem
    CPU. Rodá-los direto na coroutine recv() travaria o loop de eventos do
    asyncio — o mesmo loop que o aiortc usa para manter a conexão WebRTC viva
    (respostas a checks de "consent freshness" do ICE, envio de RTP, etc.).
    Por isso essa parte roda numa thread dedicada via run_in_executor,
    mantendo recv() de fato assíncrona.

    A largura de saída é sempre calculada a partir da altura do preset e da
    proporção real da tela capturada — nunca estica nem corta a imagem,
    diferente de forçar uma resolução fixa (ex.: 1280x720 numa tela ultra-wide).
    """

    # Carga (tempo de processamento / orçamento de tempo por frame) acima da
    # qual o modo automático desce um degrau de qualidade.
    _AUTO_LOAD_HIGH = 0.85
    # Carga abaixo da qual, com folga sobrando, sobe um degrau.
    _AUTO_LOAD_LOW = 0.4
    # Intervalo mínimo entre ajustes automáticos, pra não oscilar (não travar
    # trocando de qualidade toda hora quando a carga fica na borda).
    _AUTO_COOLDOWN_SECONDS = 2.5

    def __init__(self, quality: str = DEFAULT_QUALITY):
        super().__init__()
        # Uma única thread dedicada: o mss (principalmente no Windows) tem
        # afinidade de thread para seus recursos internos (GDI), então a
        # instância de mss.mss() precisa ser criada e usada sempre na
        # mesma thread — daí max_workers=1 e a criação lazy dentro dela.
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._sct = None  # criado dentro da thread dedicada, no primeiro uso
        self._monitor = None
        self._time_base = fractions.Fraction(1, 90000)
        self._frame_count = 0
        self._start_time = None

        # Estado do modo automático de qualidade
        self._auto = False
        self._auto_idx = QUALITY_ORDER.index(DEFAULT_QUALITY)
        self._load_ema: Optional[float] = None
        self._last_adapt = 0.0

        self.set_quality(quality)

    def set_quality(self, quality: str):
        self._auto = (quality == "auto")
        if self._auto:
            # Começa numa qualidade intermediária e o modo automático ajusta
            # sozinho a partir daí, conforme a carga real de captura/rede.
            self._apply_quality_index(QUALITY_ORDER.index(DEFAULT_QUALITY))
            self._load_ema = None
            self._last_adapt = time.time()
        else:
            preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS[DEFAULT_QUALITY])
            self._target_h = preset["height"]
            self._fps = preset["fps"]
            self._frame_interval = 1.0 / self._fps

    def _apply_quality_index(self, idx: int):
        idx = max(0, min(idx, len(QUALITY_ORDER) - 1))
        self._auto_idx = idx
        preset = QUALITY_PRESETS[QUALITY_ORDER[idx]]
        self._target_h = preset["height"]
        self._fps = preset["fps"]
        self._frame_interval = 1.0 / self._fps

    def _adapt_quality(self, proc_time: float):
        """Ajusta a qualidade sozinha, com base no quanto do orçamento de
        tempo por frame a captura+conversão consumiu. Cooldown evita ficar
        subindo/descendo em loop (o que travaria a imagem em vez de ajudar).
        """
        load = proc_time / self._frame_interval
        self._load_ema = load if self._load_ema is None else (self._load_ema * 0.8 + load * 0.2)

        now = time.time()
        if now - self._last_adapt < self._AUTO_COOLDOWN_SECONDS:
            return

        if self._load_ema > self._AUTO_LOAD_HIGH and self._auto_idx > 0:
            self._apply_quality_index(self._auto_idx - 1)
            self._last_adapt = now
            self._load_ema = None
            log.info("Automático: reduzindo para %s (carga alta)", QUALITY_ORDER[self._auto_idx])
        elif self._load_ema < self._AUTO_LOAD_LOW and self._auto_idx < len(QUALITY_ORDER) - 1:
            self._apply_quality_index(self._auto_idx + 1)
            self._last_adapt = now
            self._load_ema = None
            log.info("Automático: aumentando para %s (carga baixa)", QUALITY_ORDER[self._auto_idx])

    def _draw_cursor(self, pil_img):
        """Desenha um cursor de seta na posição atual do mouse, escalada para
        o tamanho do frame de saída. Sem isso, o mouse fica "invisível" no
        vídeo (mss captura a tela, mas não o cursor do sistema)."""
        try:
            cx, cy = pyautogui.position()
        except Exception:
            return

        src_w = self._monitor["width"]
        src_h = self._monitor["height"]
        rel_x = cx - self._monitor["left"]
        rel_y = cy - self._monitor["top"]
        if not (0 <= rel_x < src_w and 0 <= rel_y < src_h):
            return  # cursor está em outro monitor

        scale = pil_img.height / src_h
        x, y = rel_x * scale, rel_y * scale

        from PIL import ImageDraw
        s = max(10, int(pil_img.height * 0.035))  # cursor proporcional à altura do frame
        points = [
            (x, y),
            (x, y + s),
            (x + s * 0.35, y + s * 0.75),
            (x + s * 0.55, y + s * 1.0),
            (x + s * 0.72, y + s * 0.88),
            (x + s * 0.5, y + s * 0.6),
            (x + s * 0.85, y + s * 0.52),
        ]
        draw = ImageDraw.Draw(pil_img)
        draw.polygon(points, fill=(255, 255, 255), outline=(0, 0, 0))

    def _capture_and_convert(self):
        """Roda inteiramente na thread dedicada: captura, redimensiona
        preservando a proporção real da tela e desenha o cursor."""
        if self._sct is None:
            self._sct = mss.mss()
            self._monitor = self._sct.monitors[1]  # monitor principal

        raw = self._sct.grab(self._monitor)
        img = np.array(raw)[:, :, :3]  # BGRA -> BGR (descarta alpha)
        src_h, src_w = img.shape[:2]

        target_h = self._target_h
        # Largura calculada a partir da altura do preset + proporção real da
        # tela — nunca estica (não fica "achatado") nem corta a imagem.
        target_w = max(2, int(round(src_w * (target_h / src_h) / 2)) * 2)

        from PIL import Image
        pil_img = Image.fromarray(img[:, :, ::-1])  # BGR -> RGB (PIL espera RGB)
        pil_img = pil_img.resize((target_w, target_h), Image.BILINEAR)

        self._draw_cursor(pil_img)

        out = np.ascontiguousarray(np.array(pil_img)[:, :, ::-1])  # RGB -> BGR de volta
        return VideoFrame.from_ndarray(out, format="bgr24")

    async def recv(self):
        if self._start_time is None:
            self._start_time = time.time()

        # Ritmo de captura de acordo com o fps do preset atual
        next_frame_time = self._start_time + self._frame_count * self._frame_interval
        now = time.time()
        if next_frame_time > now:
            await asyncio.sleep(next_frame_time - now)

        loop = asyncio.get_event_loop()
        t0 = time.time()
        frame = await loop.run_in_executor(self._executor, self._capture_and_convert)
        proc_time = time.time() - t0

        if self._auto:
            self._adapt_quality(proc_time)

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
                if not is_valid_quality(quality):
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
                if is_valid_quality(q):
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
# USB (ADB forward automático) — sem precisar digitar comando no terminal
# --------------------------------------------------------------------------

USB_STATUS_LABELS = {
    "checking":    ("Cabo USB: verificando...", "gray"),
    "connected":   ("Cabo USB: pronto ✅ (pode conectar)", "#2e7d32"),
    "no_device":   ("Cabo USB: plugue o cabo e autorize a depuração", "gray"),
    "adb_missing": ("Cabo USB: 'adb' não encontrado no PC", "gray"),
    "error":       ("Cabo USB: erro ao verificar dispositivo", "gray"),
}


def _adb_forward_loop():
    """Roda em background pra sempre. Assim que detecta um celular Android
    com depuração USB ativa e autorizada, aplica sozinho
    `adb forward tcp:PORT tcp:PORT` — o usuário só precisa plugar o cabo e
    tocar em "Permitir" na depuração USB do celular, sem digitar nada no
    terminal do PC. Precisa do "adb" (Android SDK Platform Tools) instalado
    e no PATH do sistema — o mesmo requisito de qualquer depuração USB.
    """
    import shutil
    import subprocess

    adb = shutil.which("adb")
    if adb is None:
        log.info("adb não encontrado no PATH; modo USB automático desativado (Wi-Fi/QR continuam normais).")
        STATE.usb_status = "adb_missing"
        return

    while True:
        try:
            result = subprocess.run(
                [adb, "devices"], capture_output=True, text=True, timeout=5,
            )
            lines = [ln for ln in result.stdout.splitlines()[1:] if ln.strip()]
            devices = [ln.split("\t")[0] for ln in lines if ln.endswith("\tdevice")]

            if devices:
                subprocess.run(
                    [adb, "forward", f"tcp:{PORT}", f"tcp:{PORT}"],
                    capture_output=True, text=True, timeout=5,
                )
                STATE.usb_status = "connected"
            else:
                # "unauthorized" (falta aceitar no celular) cai aqui também;
                # a mensagem já orienta a autorizar a depuração.
                STATE.usb_status = "no_device"
        except Exception as exc:
            log.debug("Falha ao checar/configurar adb forward: %s", exc)
            STATE.usb_status = "error"

        time.sleep(3)


def start_usb_autoforward():
    threading.Thread(target=_adb_forward_loop, daemon=True).start()


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

def start_ui(hostname: str):
    """Janela Tkinter simples rodando em thread separada.

    Mostra o PIN atual, o QR Code de conexão e um checkbox que liga/desliga
    STATE.allow_control. Roda em thread própria para não bloquear o loop
    assíncrono do servidor.
    """
    import sys
    import tkinter as tk

    def _icon_path() -> str:
        # Quando empacotado pelo PyInstaller, os "datas" ficam em sys._MEIPASS;
        # rodando via "python3 server.py" direto, usamos a pasta ao lado do script.
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, "assets", "icon.png")

    def _build_qr_photo(root):
        """Gera o QR Code de conexão como PhotoImage do Tkinter.

        O QR carrega host/porta/nome/PIN em JSON: quem escaneia pelo app
        conecta direto, sem precisar digitar o PIN na mão. Como só é visível
        fisicamente na tela do PC, isso mantém o mesmo nível de segurança de
        alguém lendo o PIN e digitando manualmente.
        """
        try:
            import qrcode
            from PIL import ImageTk

            payload = json.dumps({
                "host": get_local_ip(),
                "port": PORT,
                "name": hostname,
                "pin": STATE.pin,
            })
            qr_img = qrcode.make(payload, box_size=5, border=2)
            return ImageTk.PhotoImage(qr_img, master=root)
        except Exception as exc:
            log.warning("Não foi possível gerar o QR code (%s).", exc)
            return None

    def run():
        nonlocal_state = {"log_window": None}

        root = tk.Tk()
        root.title(APP_NAME)
        root.geometry("340x620")
        root.resizable(False, False)

        def shutdown():
            # Fecha o processo inteiro (inclusive o servidor assíncrono que
            # roda na thread principal) — sem isso, fechar só a janela deixa
            # o NuDuck Server "fantasma" rodando em segundo plano, ainda
            # ocupando a porta 8765.
            log.info("Encerrando o %s...", APP_NAME)
            try:
                root.destroy()
            except Exception:
                pass
            os._exit(0)

        root.protocol("WM_DELETE_WINDOW", shutdown)

        def open_log_viewer():
            win = nonlocal_state["log_window"]
            if win is not None and win.winfo_exists():
                win.lift()
                return

            import tkinter.scrolledtext as scrolledtext

            win = tk.Toplevel(root)
            win.title(f"{APP_NAME} — Terminal")
            win.geometry("640x420")

            text = scrolledtext.ScrolledText(
                win, bg="#0b0b0b", fg="#e6e6e6", insertbackground="#e6e6e6", font=("Consolas", 9),
            )
            text.pack(fill="both", expand=True)
            text.configure(state="disabled")

            def poll_logs():
                updated = False
                while True:
                    try:
                        line = LOG_QUEUE.get_nowait()
                    except queue.Empty:
                        break
                    text.configure(state="normal")
                    text.insert("end", line + "\n")
                    updated = True
                if updated:
                    # mantém só as últimas ~1000 linhas pra não crescer pra sempre
                    n_lines = int(text.index("end-1c").split(".")[0])
                    if n_lines > 1000:
                        text.delete("1.0", f"{n_lines - 1000}.0")
                    text.see("end")
                    text.configure(state="disabled")
                if win.winfo_exists():
                    win.after(400, poll_logs)

            poll_logs()
            nonlocal_state["log_window"] = win

        try:
            icon_img = tk.PhotoImage(file=_icon_path())
            root.iconphoto(True, icon_img)
        except Exception as exc:
            log.warning("Não foi possível carregar o ícone da janela (%s).", exc)

        tk.Label(root, text=APP_NAME, font=("Sans", 16, "bold")).pack(pady=(15, 5))
        tk.Label(root, text="PIN de conexão:", font=("Sans", 11)).pack()
        tk.Label(root, text=STATE.pin, font=("Sans", 28, "bold")).pack(pady=(0, 10))

        qr_photo = _build_qr_photo(root)
        if qr_photo is not None:
            qr_label = tk.Label(root, image=qr_photo)
            qr_label.image = qr_photo  # mantém referência (evita garbage collection)
            qr_label.pack(pady=(0, 5))
            tk.Label(
                root,
                text="Escaneie no app para conectar sem digitar o PIN",
                font=("Sans", 9),
                fg="gray",
            ).pack(pady=(0, 10))

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

        usb_label = tk.Label(root, text="Cabo USB: verificando...", fg="gray", font=("Sans", 9))
        usb_label.pack(pady=(8, 0))

        def poll_usb_status():
            text, color = USB_STATUS_LABELS.get(STATE.usb_status, USB_STATUS_LABELS["checking"])
            usb_label.config(text=text, fg=color)
            root.after(1500, poll_usb_status)

        poll_usb_status()

        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=(12, 0))
        tk.Button(btn_frame, text="Ver terminal", command=open_log_viewer).pack(side="left", padx=5)
        tk.Button(
            btn_frame, text="Encerrar servidor", command=shutdown, fg="white", bg="#b91c1c",
        ).pack(side="left", padx=5)

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


def _port_available(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    hostname = socket.gethostname().split(".")[0]

    if not _port_available(PORT):
        log.error(
            "Porta %d já está em uso — provavelmente outra instância do %s "
            "já está rodando em segundo plano.", PORT, APP_NAME,
        )
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                APP_NAME,
                f"Já existe um {APP_NAME} rodando em segundo plano "
                f"(porta {PORT} ocupada).\n\n"
                "Feche a outra instância antes de abrir uma nova:\n"
                "• Linux/macOS: pkill -f NuDuck-Server\n"
                "• Windows: encerre pelo Gerenciador de Tarefas.\n\n"
                "Dica: use o botão \"Encerrar servidor\" na janela do NuDuck "
                "(em vez de só fechar a janela no X) pra evitar isso da "
                "próxima vez.",
            )
            root.destroy()
        except Exception:
            pass
        return

    print("=" * 50)
    print(f"  {APP_NAME}")
    print("=" * 50)
    print(f"  PIN de conexão: {STATE.pin}")
    print(f"  Porta: {PORT}")
    print("  Modo Wi-Fi: conecte pelo app Android na mesma rede local.")
    print("             (ou escaneie o QR Code exibido na janela)")
    print(f"  Modo USB:   plugue o cabo com a depuração USB ativa —")
    print(f"              o forward (tcp:{PORT}) é feito sozinho, sem terminal.")
    print("=" * 50)

    try:
        start_ui(hostname)
    except Exception as exc:
        log.warning("Interface gráfica indisponível (%s); use somente o console.", exc)

    start_usb_autoforward()

    zeroconf = start_mdns(hostname)
    app = build_app()

    try:
        web.run_app(app, host="0.0.0.0", port=PORT, print=None)
    finally:
        zeroconf.close()


if __name__ == "__main__":
    main()
