# DroidMonitor

Estende a tela de um PC Linux para um celular Android via WebRTC, na rede
local. Sem nuvem, sem cadastro/login — apenas PIN local.

## Estrutura

```
droidmonitor/
├── server/                 # servidor Python (roda no PC Linux)
│   ├── server.py
│   └── requirements.txt
└── android/                 # app cliente Android (Kotlin + Compose)
    ├── app/
    │   ├── build.gradle.kts
    │   └── src/main/...
    ├── build.gradle.kts
    └── settings.gradle.kts
```

## Rodando o servidor (PC Linux)

```bash
cd server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 server.py
```

Uma janela mostra o PIN de 6 dígitos e o checkbox "Permitir controle".
Deixe desmarcado para modo somente-espelho.

> No Linux, captura de tela com `mss` e controle com `pyautogui` funcionam
> nativamente em X11. Em Wayland pode ser necessário rodar a sessão em
> Xorg (ou usar XWayland) para que `pyautogui` consiga mover o mouse/teclar.

## Rodando o cliente (Android)

1. Abra a pasta `android/` no Android Studio (Koala ou mais recente).
2. Deixe o Gradle sincronizar (baixa `google-webrtc`, `jmdns`, `okhttp`, Compose).
3. Rode no celular (MinSDK 24 / Android 7.0+).

### Modo Wi-Fi
Celular e PC na mesma rede. O app lista os PCs encontrados via mDNS
(`_droidmonitor._tcp.local.`). Toque no PC, digite o PIN mostrado na tela dele.

### Modo USB (via ADB)
```bash
adb forward tcp:8765 tcp:8765
```
Nesse modo o mDNS não funciona pelo cabo; adicione manualmente o PC como
`127.0.0.1:8765` (é possível estender a tela de descoberta com um campo
"conectar manualmente" caso deseje — a base de sinalização já aceita
loopback para viabilizar exatamente esse cenário).

## Protocolo de sinalização (WebSocket em `/ws`, porta 8765)

```
Android -> PC   {"type":"pin","pin":"123456"}
PC -> Android   {"type":"pin_ok"}  |  {"type":"pin_error","blocked":bool}

Android -> PC   {"type":"offer","sdp":"...","sdpType":"offer","quality":"media"}
PC -> Android   {"type":"answer","sdp":"...","sdpType":"answer"}

Android -> PC   {"type":"quality","value":"alta"}   # troca de qualidade em runtime
```

Depois do handshake WebRTC, o vídeo trafega por uma track de mídia
(DTLS/SRTP obrigatórios) e os toques trafegam por um `DataChannel("control")`:

```
Android -> PC   {"type":"down","x":0.42,"y":0.71}
Android -> PC   {"type":"move","x":0.43,"y":0.70}
Android -> PC   {"type":"up","x":0.43,"y":0.70}
Android -> PC   {"type":"key","key":"enter"}
```

O servidor só executa esses eventos com `pyautogui` se o checkbox
"Permitir controle" estiver marcado; caso contrário, apenas ignora — o
espelhamento de vídeo continua funcionando normalmente.

## Segurança

- Sem PIN correto, o servidor não aceita nenhuma oferta WebRTC.
- 5 tentativas de PIN erradas bloqueiam o IP por 60s.
- Middleware do servidor rejeita qualquer IP fora de `192.168.x`, `10.x`,
  `172.16-31.x` ou `127.0.0.1` (loopback liberado propositalmente para
  viabilizar `adb forward`).
- WebRTC (aiortc e google-webrtc) usa DTLS/SRTP por padrão — não há como
  negociar mídia sem criptografia nessas pilhas.
- Controle de mouse/teclado só é executado se o checkbox local estiver
  marcado; o app Android nunca pode "forçar" permissão de controle.

## Limitações conhecidas / próximos passos

- `RTCConfiguration` não usa STUN/TURN (não é necessário em LAN); redes
  com múltiplas sub-redes/roteadores mais complexos podem exigir ajuste.
- O redimensionamento de frame no servidor usa `VideoFrame.reformat`
  (via PyAV); em telas muito grandes pode valer a pena avaliar custo de
  CPU e, se necessário, trocar por um encoder por hardware.
- Não há tela de "conectar manualmente por IP" no cliente Android ainda
  (útil para o modo USB); a base para isso já existe em `SignalingClient`
  e `MainViewModel.submitPin`, bastando adicionar um `PcInfo` manual.
