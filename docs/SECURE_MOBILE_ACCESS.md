# Secure mobile access with Tailscale

Agents Chat can stay on your Mac or Windows PC while you use it from a phone.
The computer still runs the agents and keeps the files. Tailscale creates an
encrypted, private path between devices in the same tailnet.

Use **Tailscale Serve**, not **Tailscale Funnel**. Serve is restricted to the
tailnet and follows its access rules. Funnel is reachable from the public
internet and is not the recommended personal-mobile setup.

## Easiest setup

1. Open Agents Chat on the computer and go to **Settings → Secure mobile access**.
2. Install Tailscale on the computer if Agents Chat says it is missing.
3. Sign into Tailscale.
4. Select **Set up private access**. Agents Chat uses private HTTPS port `8443`
   and refuses to overwrite a route that belongs to another service.
5. Install Tailscale on the phone and sign into the same tailnet.
6. Turn Tailscale on, copy the private `https://…ts.net:8443` address from Agent
   Chat, open it on the phone, and sign into Agents Chat.
7. Bookmark or add the page to the phone's home screen.

The computer must remain awake, Agents Chat must be running, and both devices
must be connected to Tailscale.

## Let Codex or Claude Code help

The Settings card creates a copy-ready prompt containing the correct Agents Chat
folder, local port, and private HTTPS port for that installation. Open Codex or
Claude Code on the Agents Chat computer, paste the prompt, and approve each
change. The prompt requires the desktop agent to:

- inspect existing Serve and Funnel routes before changing anything;
- keep Agents Chat bound to `127.0.0.1`;
- preserve Agents Chat login protection;
- use `tailscale serve`, never `tailscale funnel`;
- avoid overwriting unrelated routes; and
- return the private phone address and walk through phone setup.

The desktop agent can inspect and configure the computer. It cannot sign into
the user's Tailscale account or phone on the user's behalf; those steps remain
with the user.

## Manual command

After Tailscale is installed and signed in, the default command is:

```sh
tailscale serve --bg --https=8443 http://127.0.0.1:8086
```

Check the result with:

```sh
tailscale serve status
tailscale funnel status
```

The Agents Chat route must say it is available within the tailnet, and Funnel
must not be on for port `8443`.

Official references:

- Tailscale downloads: https://tailscale.com/download
- Tailscale Serve: https://tailscale.com/docs/features/tailscale-serve
- Serve command: https://tailscale.com/docs/reference/tailscale-cli/serve
