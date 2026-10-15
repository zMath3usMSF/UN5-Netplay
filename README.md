# UN5 Netplay Launcher

**Language / Idioma:** [English](#-english) · [Português (BR)](#-português-br)

---

## 🇺🇸 English

Desktop launcher (Python/Tkinter) for **UN5 Netplay**, a 2-player lockstep online mode for *Naruto Shippuden: Ultimate Ninja 5* running on PCSX2. It wraps the existing `netplay.py` as a subprocess: it does not replace or reimplement the network logic.

### Features

- Login with **Discord**: your name and avatar show up in the room list and in the game.
- **Room list** with host and guest avatars, match phase (`SELECTING` / `IN FIGHT`), real ping and match time.
- **Automatic input delay**: the host picks it from its ping to the server, the guest uses the host's value.
- **Match state sync**: the default save state and `symbols.json` are downloaded from the server when yours differ.
- **Owner-only PREPARE**: the owner prepares the match state, which is then published to the server automatically.
- Extra tools: load the match state into PCSX2, solo test, PINE test and release game.

### Requirements

- Windows with Python 3.10+ (Tkinter is included in the standard Windows distribution).
- [Pillow](https://pypi.org/project/Pillow/) (optional, `pip install pillow`): without it the room list has no avatars.
- PCSX2 with **PINE enabled** (default slot `28011`).
- The netplay project files in the same folder as `launcher.py`: `netplay.py`, `core_link.py`, `node.py`, `netlink.py`, `statesync.py`, `pine.py`, `selftest_netpad.py`, `rooms.py`, `wire.py`, `lockstep.py`, `simgame.py`.
- The online server (relay / accounts) is **not** included in this repository. The address the launcher connects to is `SERVER_ADDR` in `launcher_core.py`.

### How to run

```
python launcher.py
```

or double-click `1 - Run.bat`. User data (preferences, Discord session, match state) is stored in `%APPDATA%\UN5 Netplay`.

### How to play

**Host**

1. Log in with Discord (top right).
2. Choose **HOST · P1** and click **START HOST**.
3. The launcher downloads the server's match state, loads it into PCSX2 (frozen at frame 0) and opens your room.

**Guest**

1. Log in with Discord.
2. Click an open room in the **ROOMS** list.
3. Click **CONNECT AS GUEST**.

### Notes

- Loading the match state **overwrites PCSX2 save slot 9**. Use a slot you do not care about.
- Both players need pad 2 connected in PCSX2. Only one PINE client can run at a time: close other tools during a session.
- **STOP** asks the session to end cleanly so the real pads are restored. If the game stays frozen, use **Tools → RELEASE GAME**.
- Both PCs must hold byte-identical match states; the launcher handles this by downloading the server's copy.
- Match state files (`.p2s`) and `symbols.json` are not part of the repository.

---

## 🇧🇷 Português (BR)

Launcher desktop (Python/Tkinter) do **UN5 Netplay**, um modo online lockstep para 2 jogadores de *Naruto Shippuden: Ultimate Ninja 5* rodando no PCSX2. Ele executa o `netplay.py` existente como subprocesso: não substitui nem reimplementa a lógica de rede.

### Recursos

- Login com **Discord**: seu nome e avatar aparecem na lista de salas e dentro do jogo.
- **Lista de salas** com avatar do host e do guest, fase da partida (`SELECTING` / `IN FIGHT`), ping real e tempo de partida.
- **Input delay automático**: o host escolhe pelo ping até o servidor e o guest usa o valor do host.
- **Sincronização do save state**: o save state padrão e o `symbols.json` são baixados do servidor quando os seus forem diferentes.
- **PREPARE só para o dono**: o dono prepara o estado da partida e ele é publicado no servidor automaticamente.
- Ferramentas extras: carregar o estado no PCSX2, teste solo, teste do PINE e liberar o jogo.

### Requisitos

- Windows com Python 3.10+ (o Tkinter já vem na distribuição padrão do Python para Windows).
- [Pillow](https://pypi.org/project/Pillow/) (opcional, `pip install pillow`): sem ele a lista de salas fica sem avatares.
- PCSX2 com **PINE habilitado** (slot padrão `28011`).
- Os arquivos do projeto de netplay na mesma pasta do `launcher.py`: `netplay.py`, `core_link.py`, `node.py`, `netlink.py`, `statesync.py`, `pine.py`, `selftest_netpad.py`, `rooms.py`, `wire.py`, `lockstep.py`, `simgame.py`.
- O servidor online (relay / contas) **não** está incluído neste repositório. O endereço usado pelo launcher é o `SERVER_ADDR` em `launcher_core.py`.

### Como abrir

```
python launcher.py
```

ou dê dois cliques em `1 - Run.bat`. Os dados do usuário (preferências, sessão do Discord, estado da partida) ficam em `%APPDATA%\UN5 Netplay`.

### Como jogar

**Host**

1. Faça login com o Discord (canto superior direito).
2. Escolha **HOST · P1** e clique em **START HOST**.
3. O launcher baixa o estado da partida do servidor, carrega no PCSX2 (congelado no frame 0) e abre a sua sala.

**Guest**

1. Faça login com o Discord.
2. Clique em uma sala aberta na lista **ROOMS**.
3. Clique em **CONNECT AS GUEST**.

### Observações

- Carregar o estado da partida **sobrescreve o slot 9 de save do PCSX2**. Use um slot que você não se importe de perder.
- Os dois jogadores precisam do controle 2 conectado no PCSX2. Só um cliente PINE funciona por vez: feche outras ferramentas durante a sessão.
- **STOP** pede para a sessão encerrar de forma limpa e devolver os controles reais. Se o jogo continuar congelado, use **Tools → RELEASE GAME**.
- Os dois PCs precisam ter estados de partida idênticos (byte a byte); o launcher resolve isso baixando a cópia do servidor.
- Os arquivos de estado (`.p2s`) e o `symbols.json` não fazem parte do repositório.
