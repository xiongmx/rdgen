# RDGen, a RustDesk client generator to use with your self-hosted RustDesk server

The client generator is currently hosted [here](https://rdgen.crayoneater.org).
If you would like to host the generator yourself, see [here](setup.md)

## rustdesk api server integration

The rdgen client generator is integrated into my fork of the [rustdesk-api](https://github.com/bryangerlach/rustdesk-api). This is currently a work in progress, and might have bugs.

## rdgen local build alternative

If you would prefer to build rustdesk on your own computer, check out the [DVForge](https://github.com/VenimK/DVForge) project.

## Features

- Embed server and key into client
- Custom app name
- Custom icon/logo
- Set default settings for the client
- Support for rustdesk advanced settings (https://rustdesk.com/docs/en/self-host/client-configuration/advanced-settings/)

## Generate RustDesk clients from command line instead of using a web browser

Save your configuration from the rdgen web interface, or generate your own, then use that json file with [@AlekseyLapunov's rdgen-cli](https://github.com/AlekseyLapunov/rdgen-cli) to build from the command line on Windows, Linux, or MacOS like this: `python rdgen-cli -f my_config.json --set-version 1.4.5 --set-platform windows -s https://rdgen.crayoneater.org`

## Notes

- Icons should be square (256x256 recommended)
- Avoid special characters or non-English characters in app name and file name
- Build time is currently 30 - 45 minutes

## Automated Windows x64 and Android releases

The `Build Windows x64 and Android when RDGen supports a newer RustDesk client` workflow checks the upstream RDGen version choices every six hours. It builds only after upstream RDGen lists a newer stable RustDesk version than the latest complete release in this fork; RustDesk release events alone do not trigger a build. A release is published only when both platform builds succeed.

The workflow reads its fixed server settings from the GitHub Environment named `rustdesk`: `RUSTDESK_SERVER`, `RUSTDESK_SERVER_PORT`, `RUSTDESK_API_SERVER`, and `RUSTDESK_PUBLIC_KEY`. `RDGEN_BASELINE_VERSION` records the support level at setup, so that existing supported versions do not trigger an initial build. It produces a Windows x64 EXE/MSI (when the MSI build succeeds) and Android APKs for arm64, armv7, and x86_64. It does not send build files to the RDGen web service or RustDesk API server.
