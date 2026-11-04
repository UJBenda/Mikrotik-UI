# Sestavené balíčky

Poslední sestavený `.deb` (jen jeden – starší se mažou). Instalace / aktualizace na serveru:

```sh
git pull
sudo cp packages/mtcontroller_*_all.deb /tmp/
sudo apt install /tmp/mtcontroller_*_all.deb
```

(`/tmp`, protože `_apt` nečte soubory v `/root`; `apt install` dotáhne nové závislosti.)
Sestavení ručně: `sh packaging/build-deb.sh` (výstup v `dist/`).
