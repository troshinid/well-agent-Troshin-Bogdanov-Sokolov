import argparse
import csv
import hashlib
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Опись исходных файлов")
    parser.add_argument("raw", type=Path, help="Путь к папке raw")
    args = parser.parse_args()

    raw = args.raw.resolve()
    if not raw.is_dir():
        parser.error(f"Папка не найдена: {raw}")

    files = sorted(path for path in raw.iterdir() if path.is_file())
    if not files:
        parser.error(f"Папка пуста: {raw}")

    output = raw.parent / "inventory.csv"
    hashes = {}

    with output.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["file", "size_bytes", "sha256", "duplicate_of"])

        for path in files:
            checksum = sha256(path)
            duplicate = hashes.get(checksum, "")
            writer.writerow([
                path.name,
                path.stat().st_size,
                checksum,
                duplicate,
            ])
            hashes.setdefault(checksum, path.name)

    print(f"Файлов: {len(files)}")
    print(f"Опись сохранена: {output}")


if __name__ == "__main__":
    main()
