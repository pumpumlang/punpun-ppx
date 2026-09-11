<p align="center"><img src="static/ppx-mark.svg" width="92" alt="PPX"></p>

# PunPunXPac · PPX

<p align="center"><strong>The package manager for PunPun, and the catalog that fronts it.</strong></p>

This repository holds the PPX client, the reference registry server, and the
public catalog site. The language itself lives in
[`pumpumlang/punpun`](https://github.com/pumpumlang/punpun); its documentation
lives in [`pumpumlang/punpun-docs`](https://github.com/pumpumlang/punpun-docs).

## Layout

| Path | Contents |
| --- | --- |
| `ppx/` | The PPX command-line client |
| `registry/` | Reference registry server and its schema |
| `src/`, `static/` | Catalog site templates and served assets |
| `build.py` | The catalog site generator |
| `*.html` | Generated output, committed for GitHub Pages |

## Install

PPX needs Python 3.11+ and a PunPun toolchain on `PATH`. Put `ppx/ppx` on your
`PATH`, or call it directly:

```sh
./ppx/ppx doctor
```

`ppx doctor` reports where PPX found `pp` and the first-party package set.

### Finding the language

PPX resolves the first-party packages that ship with the language, in order:

1. `PUNPUN_PACKAGES` — a package directory
2. `PUNPUN_ROOT` — a PunPun checkout or install prefix
3. a `punpun` checkout beside this one
4. the installed toolchain (`…/lib/punpun/packages`)

`PUNPUN_PP` overrides the `pp` executable the same way.

## Everyday commands

```sh
ppx search requests
ppx info requests
ppx install requests
ppx tree
ppx audit
pp run
```

Local development dependencies are supported with:

```sh
ppx add my_package --path ../my_package
```

PPX uses the same `Punpun.toml` / `Punpun.lock` package graph consumed by `pp`.
There is no package-manager-specific compiler pipeline.

## Publishing

A public package uses normal semantic version requirements in `[dependencies]`.
Local path dependencies are rejected when publishing because they are not
reproducible on another machine. See the
[publishing guide](https://pumpumlang.github.io/punpun-docs/ppx-publishing.html).

Package archives can carry detached Ed25519 signatures:

```sh
ppx sign my_package-0.1.0.zip --private-key signing.pem
ppx trust add signing.pub
```

Signing shells out to OpenSSL, so PPX stays dependency-free.

## Registry server

`registry/server.py` is the reference implementation used for development and
for the publishing walkthrough:

```sh
python3 registry/server.py --host 127.0.0.1 --port 8765 --data .ppx-registry
export PPX_REGISTRY=http://127.0.0.1:8765
```

PPX refuses a non-loopback registry over plain HTTP unless
`PPX_ALLOW_INSECURE_REGISTRY=1` is set for an explicitly trusted development
endpoint.

## Catalog site

```sh
python3 build.py --punpun-root ../punpun
python3 -m http.server 8080
```

`build.py` renders `src/*.html` to the repository root and regenerates
`static/catalog.json` from each `packages/*/Punpun.toml` in the PunPun
checkout. Without a checkout it keeps the committed catalog, so the site
always builds. `python3 build.py --check` fails when the committed output has
drifted; CI runs it on every push and pull request.

| Mode | Data source | Server required |
| --- | --- | --- |
| Public/default | Generated `static/catalog.json` | No |
| Registry development | Explicit `ppxRegistry` browser override | Yes |
| Offline preview | Same generated catalog through a local static server | No |

To point the site at a live PPX API, set an endpoint in the browser console and
reload:

```js
localStorage.setItem('ppxRegistry', 'https://your-registry.example')
```

Remove the override to return to the bundled catalog:

```js
localStorage.removeItem('ppxRegistry')
```

If a live endpoint is unreachable the site falls back to the bundled catalog
rather than showing a broken page. No access token or registry credential
belongs in this static site.
