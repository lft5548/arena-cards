"""Install pinned official NuGet runtime assemblies for Unity .NET Standard 2.1."""
from __future__ import annotations

import io
import urllib.request
import xml.etree.ElementTree as xml
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (
    ("Google.Protobuf", "3.21.12", "lib/netstandard2.0/Google.Protobuf.dll"),
    ("System.Runtime.CompilerServices.Unsafe", "4.5.2",
     "lib/netstandard2.0/System.Runtime.CompilerServices.Unsafe.dll"),
)


def main() -> None:
    cache = ROOT / "build-protobuf-dependencies"
    destination = ROOT / "client_unity/Assets/Plugins/Protobuf"
    cache.mkdir(exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    namespace = {"package": "http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd"}
    for package, version, library in PACKAGES:
        filename = f"{package.lower()}.{version}.nupkg"
        path = cache / filename
        if not path.is_file():
            url = f"https://api.nuget.org/v3-flatcontainer/{package.lower()}/{version}/{filename}"
            with urllib.request.urlopen(url, timeout=30) as response:
                payload = response.read()
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                archive.getinfo(library)
            path.write_bytes(payload)
        with zipfile.ZipFile(path) as archive:
            metadata = xml.fromstring(archive.read(package + ".nuspec"))
            if (metadata.findtext("package:metadata/package:id", namespaces=namespace) != package or
                    metadata.findtext("package:metadata/package:version", namespaces=namespace) != version):
                raise ValueError("unexpected NuGet package identity")
            (destination / Path(library).name).write_bytes(archive.read(library))
        print(f"installed {package} {version}")


if __name__ == "__main__":
    main()
