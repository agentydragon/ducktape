My homepage. Everything is CC BY-NC-SA 3.0.

Hakyll (`site.hs`) renders it into an OCI image served by `static-web-server`
(`default.nix`). The cluster runs it (`cluster/cdk8s/website/website.py`); a push to
`devel` that touches this directory publishes a new image, and Flux rolls it out.

# Important linked URLs

| URL                                   | purpose                              |
| ------------------------------------- | ------------------------------------ |
| http(s)://adgn.link/found             | lost-and-found stickers, v2          |
| http(s)://adgn.link/nfc               | NFC armband                          |
| http(s)://agentydragon.com/found.html | lost-and-found stickers, v1 (legacy) |

# Develop

From the repo root:

```bash
sass website/css/default.scss > website/css/default.css  # after editing the SCSS
nix build .#website-image
docker load < result && docker run --rm -p 8080:8080 website:latest
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
