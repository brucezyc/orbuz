# Orbuz independent test workcell

Provisioned 2026-09-07. No real model task has been started.

- PVE: `192.168.0.100`, container `111`, hostname `orbuz-test`.
- Debian 12, unprivileged, nesting/keyctl enabled; 2 cores, 4096 MiB RAM, 512 MiB swap, 30 GiB disk. Onboot disabled.
- DHCP observed address: `192.168.0.24`. Rediscover with `ssh root@192.168.0.100 'pct exec 111 -- ip -br -4 addr'`.
- Hermes SSH: `ssh -o HostKeyAlias=orbuz-test-111 root@192.168.0.24`. Host public key was obtained via authenticated PVE, not accepted blindly. Only SSH public authorization was installed; no private Git/host keys copied.
- Source: `/root/orbuz`, GitHub `brucezyc/orbuz`, branch `feat/evidence-runtime`, detached reviewed revision. Latest tested source: `6585fc0`.
- Python environment: `/root/venv`; Rust minimal stable toolchain installed.
- Empty task area: `/root/projects`; evidence state: `/root/orbuz-state`.
- Model configuration: `/root/.orbuz/forge.yaml`, mode 0600, model/key/api_base fields copied from 109. Webhooks and unrelated settings omitted. Existing containers were not modified.
- Observed legacy configuration: quality/balanced/cheap specify `deepseek/deepseek-v4-flash`; architect has no explicit model. All tiers can obtain a key via tier/global fields. Credential presence is not API authentication evidence.

## Verified (offline only)

`/root/yzhu/exports/orbuz-lxc111-verify.log` records:

- Real sandbox execution: `SANDBOX_OK`, exit 0; config path and DEEPSEEK_API_KEY unavailable inside sandbox.
- Actual deployed test suite: **106 passed in 11.19s**.
- `pip check`: no broken requirements. Both CLI help entry points run.
- SSH hostname and DHCP address checked; Rust reports `rustc 1.98.1`.

The first installation attempt ran before DHCP DNS was ready and failed. Original log retained at `/root/yzhu/exports/orbuz-lxc111-provision.log`. The committed script adds bounded DNS readiness and `APT::Update::Error-Mode=any`, then resumed installation without recreating the container. Successful installation log: `/root/yzhu/exports/orbuz-lxc111-install.log`.

## Choosing the actual trial

The new `python -m orbuz.runtime` entry implements one bounded task with native tools and fixed independent acceptance. It does NOT read four-tier `forge.yaml`: pass explicit model/base-url and key environment per RUNTIME.md. `orbuz run --config /root/.orbuz/forge.yaml` is the separate preserved legacy multi-agent entry. Do not imply that tests of the new core certify the legacy orchestration chain or autonomous large-project delivery.

Before a live run: choose the real deliverable, acceptance, model endpoint and request/token/time limits. Resolve architect configuration if using the legacy workflow. Prepare task requirements, not the implementation. Hermes may independently test artifacts but must disclose any rescue edits. No automatic retry service, cron or paid completion was enabled during setup.
