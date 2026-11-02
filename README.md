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

The `Build Windows x64 and Android when RDGen supports a newer RustDesk client` workflow checks upstream RDGen's supported version choices every six hours. It uses the newest stable version that RDGen actually supports; a RustDesk release event alone does not trigger a build. A release is published only after both target builds succeed. The configured target scope is Windows x64 EXE and Android armv7 APK, with versioned release filenames. Docker builds remain disabled.

The private GitHub Environment named `rustdesk` must contain `RUSTDESK_CLIENT_CONFIG_JSON_B64`, a base64-encoded saved RDGen JSON configuration, and `RUSTDESK_CUSTOM_CLIENT_SIGNING_PRIVATE_KEY`, an Ed25519 PKCS#8 PEM key used to sign the custom-client settings. The private key is used only by the config-preparation job; downstream build jobs receive the signed payload and its public key. The compiled client retains RustDesk's signature verification. `RDGEN_BASELINE_VERSION` remains the version watermark and prevents an initial rebuild of an already-supported version. The version in the saved JSON is ignored; the build version comes from RDGen's current supported-version list.

The settings layer follows the saved JSON. For example, `permissionsDorO=default` puts those values in RustDesk's `default-settings` layer, overriding built-in defaults but not existing user preferences or higher-priority server strategies. To enforce settings over user choices, the saved configuration must explicitly select `override`. The `cycleMonitor` field is not supported by the current RDGen form or RustDesk 1.5.0 source; its current saved value is false and has no build effect. Build files are not sent to the RDGen web service or RustDesk API server.
