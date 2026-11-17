#!/usr/bin/env python3
"""
Convertit une ou plusieurs vidéos YouTube en MP3 (meilleure qualité audio).

Usage :
    python3 yt2mp3.py "https://www.youtube.com/watch?v=XXXX"
    python3 yt2mp3.py urls.txt
    python3 yt2mp3.py url1 url2 urls.txt -o ~/Musique
    python3 yt2mp3.py --normalize ~/Musique      # renomme les .mp3 existants

Le fichier de liste contient une URL par ligne (lignes vides et # ignorées).
Dépendances : pip install -U yt-dlp   +   ffmpeg (sudo apt install ffmpeg)
"""
import argparse
import os
import re
import signal
import sys
import time
import unicodedata

try:
    import yt_dlp
except ImportError:
    sys.exit("yt-dlp manquant : pip install -U yt-dlp")


# Lettres sans décomposition Unicode -> équivalent ASCII
TRANSLIT = str.maketrans({
    "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th", "æ": "ae", "Æ": "AE",
    "ø": "o", "Ø": "O", "œ": "oe", "Œ": "OE", "ß": "ss", "ł": "l", "Ł": "L",
    "đ": "d", "Đ": "D", "ı": "i", "&": "and",
    "–": "-", "—": "-", "‐": "-", "’": "'", "‘": "'", "“": "", "”": "",
})
MAX_NAME = 100  # longueur max du nom (hors extension)


def normalize_name(name):
    """Nom de fichier sûr pour baladeurs / clés FAT32 :
    ASCII seulement, sans caractères interdits, espaces propres, longueur limitée."""
    name = unicodedata.normalize("NFKC", name)          # ｜ pleine chasse -> |
    name = name.translate(TRANSLIT)
    name = unicodedata.normalize("NFKD", name)           # é -> e + accent
    name = name.encode("ascii", "ignore").decode()       # retire accents / emoji
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " - ", name)  # interdits FAT32/NTFS
    name = re.sub(r"[^\w\s.,()\[\]'!+=-]", "", name)     # autres symboles exotiques
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"(\s*-\s*){2,}", " - ", name)         # " -  - " -> " - "
    name = name.strip(" .-_")
    if len(name) > MAX_NAME:
        name = name[:MAX_NAME].rstrip(" .-_")
    return name or "sans_titre"


def safe_rename(path):
    """Renomme le fichier avec un nom normalisé (sans écraser). Renvoie le nouveau chemin."""
    folder, fname = os.path.split(path)
    base, ext = os.path.splitext(fname)
    new_base = normalize_name(base)
    target = os.path.join(folder, new_base + ext.lower())
    if target == path:
        return path
    n = 2
    while os.path.exists(target):
        target = os.path.join(folder, f"{new_base} ({n}){ext.lower()}")
        n += 1
    os.rename(path, target)
    print(f"  Renommé : {fname}  ->  {os.path.basename(target)}")
    return target


def normalize_existing(paths):
    """Mode --normalize : renomme les .mp3 existants (fichiers ou dossiers)."""
    count = 0
    for p in paths:
        files = ([os.path.join(p, f) for f in sorted(os.listdir(p))]
                 if os.path.isdir(p) else [p])
        for f in files:
            if f.lower().endswith(".mp3") and os.path.isfile(f):
                if safe_rename(f) != f:
                    count += 1
    print(f"\n{count} fichier(s) renommé(s).")


def collect_urls(inputs):
    urls = []
    for item in inputs:
        if os.path.isfile(item):
            with open(item, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        urls.append(line)
        else:
            urls.append(item)
    # dédoublonne en gardant l'ordre
    return list(dict.fromkeys(urls))


def on_sigint(sig, frame):
    """Ctrl+C : arrête immédiatement le script ET ses processus enfants
    (ffmpeg, deno...), sans laisser yt-dlp avaler l'interruption ni relancer."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print("\n\nInterrompu (Ctrl+C) — arrêt de tous les processus.", flush=True)
    try:
        for pid in children_pids(os.getpid()):
            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    os._exit(130)


def children_pids(ppid):
    """Tous les descendants d'un processus (lecture de /proc, Linux)."""
    kids = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/stat") as f:
                    parent = int(f.read().rsplit(")", 1)[1].split()[1])
                kids.setdefault(parent, []).append(int(d))
            except (OSError, ValueError, IndexError):
                pass
    out, stack = [], [ppid]
    while stack:
        for c in kids.get(stack.pop(), []):
            out.append(c)
            stack.append(c)
    return out


IMG_EXT =(".webp", ".jpg", ".jpeg", ".png")


def images_in(folder):
    return {f for f in os.listdir(folder) if f.lower().endswith(IMG_EXT)}


def download_with_retry(url, opts, outdir, retries):
    """YouTube renvoie parfois des 403 aléatoires : on relance l'extraction
    (nouvelle URL de flux) plusieurs fois avant d'abandonner."""
    for attempt in range(1, retries + 1):
        before = images_in(outdir)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ok = ydl.download([url]) == 0
        except Exception as e:
            print(f"  Erreur : {e}")
            ok = False
        if ok:
            return True
        # supprime les miniatures orphelines laissées par l'échec
        for f in images_in(outdir) - before:
            try:
                os.remove(os.path.join(outdir, f))
            except OSError:
                pass
        if attempt < retries:
            wait = 5 * attempt
            print(f"  Échec (tentative {attempt}/{retries}), nouvel essai dans {wait}s...")
            time.sleep(wait)
    return False


def main():
    signal.signal(signal.SIGINT, on_sigint)
    p =argparse.ArgumentParser(description="YouTube -> MP3 (best audio)")
    p.add_argument("inputs", nargs="+", help="URL(s) YouTube et/ou fichier(s) texte de liste d'URLs")
    p.add_argument("-o", "--output", default=".", help="Dossier de sortie (défaut : dossier courant)")
    p.add_argument("--playlist", action="store_true",
                   help="Télécharger toute la playlist si l'URL en contient une")
    p.add_argument("-r", "--retries", type=int, default=4,
                   help="Nombre de tentatives par URL (défaut : 4)")
    p.add_argument("--normalize", action="store_true",
                   help="Ne télécharge rien : normalise les noms des .mp3 existants "
                        "(les arguments sont alors des fichiers ou dossiers)")
    p.add_argument("--keep-names", action="store_true",
                   help="Garder les titres YouTube bruts (pas de normalisation)")
    args = p.parse_args()

    if args.normalize:
        normalize_existing(args.inputs)
        return

    urls = collect_urls(args.inputs)
    if not urls:
        sys.exit("Aucune URL trouvée.")
    os.makedirs(args.output, exist_ok=True)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(args.output, "%(title)s.%(ext)s"),
        "noplaylist": not args.playlist,
        "ignoreerrors": True,
        "writethumbnail": True,
        "postprocessors": [
            # "0" = VBR meilleure qualité (~245 kbps) ; mettre "320" pour du CBR 320k
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail"},
        ],
    }
    if not args.keep_names:
        # appelé avec le fichier final, après conversion + tags + pochette
        opts["post_hooks"] = [safe_rename]

    failed = []
    for i, url in enumerate(urls, 1):
        print(f"\n[{i}/{len(urls)}] {url}")
        if not download_with_retry(url, opts, args.output, args.retries):
            failed.append(url)

    print(f"\nTerminé : {len(urls) - len(failed)}/{len(urls)} réussi(s).")
    if failed:
        print("Échecs :")
        for u in failed:
            print("  ", u)
        sys.exit(1)


if __name__ == "__main__":
    main()
