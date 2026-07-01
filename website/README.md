My homepage. Everything is CC BY-NC-SA 3.0.

Uses Haskell/Hakyll tooling.

Hosted on my VPS.

# Important linked URLs

| URL                                   | purpose                              |
| ------------------------------------- | ------------------------------------ |
| http(s)://adgn.link/found             | lost-and-found stickers, v2          |
| http(s)://adgn.link/nfc               | NFC armband                          |
| http(s)://agentydragon.com/found.html | lost-and-found stickers, v1 (legacy) |

# Develop

`//website` is a nested Bazel module (its own `MODULE.bazel`), listed in the
root `.bazelignore` so the root workspace doesn't need `rules_haskell` /
GHC just to expand `//...`. Build the site from inside this directory:

```bash
cd website
sass css/default.scss > css/default.css
bazelisk build //:site
./bazel-bin/site build
```

To push:

```bash
rsync -av --progress _site/ vps.agentydragon.com:/var/www/agentydragon.com
```

To convert a Jupyter notebook into Markdown:

```bash
sudo apt install jupyter-nbconvert
jupyter-nbconvert --to markdown --template basic <...>.ipynb
```

# Syntax highlighting

See: <https://pandoc.org/MANUAL.html#syntax-highlighting>

It uses [skylighting](https://github.com/jgm/skylighting) library under the hood.

```bash
pandoc --list-highlighted-languages
```

```bash
stack install --flag skylighting-core:executable skylighting-core
```

```bash
skylighting --help
```

# Upgrades

Might need:

```bash
stack upgrade
```

Maybe try to upgrade resolver in `stack.yaml`.
