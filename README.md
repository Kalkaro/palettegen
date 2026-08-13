# Palette Generator

A local web app that fetches safe Konachan wallpapers and generates Base16 color palettes with Stylix.

Run it with:

```sh
nix run github:Kalkaro/palettegen

```
Connect to it: localhost:8766

Generated wallpapers and palettes are stored together in one directory:
`$XDG_DATA_HOME/palette-generator`, or `~/.local/share/palette-generator` when
`XDG_DATA_HOME` is not set.
