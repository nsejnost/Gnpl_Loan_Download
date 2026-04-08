{ pkgs }: {
  deps = [
    pkgs.python3
    pkgs.fontconfig
    pkgs.freetype
    pkgs.gdk-pixbuf
    pkgs.xorg.libXrender
  ];
}
