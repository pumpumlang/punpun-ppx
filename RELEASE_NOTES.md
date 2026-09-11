# PunPunXPac 1.4.5

PPX 1.4.5 is the package-manager companion to PunPun 1.4.5. The SDK release
embeds this exact client and refuses to assemble when the language and PPX
versions differ.

The release adds compiler-path integration through `ppx paths`, makes
`ppx outdated` report useful current/latest information (including JSON for
tooling), and keeps deterministic format-1 lockfiles and package archives
compatible with the stable 1.x line.
