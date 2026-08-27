# Bundled components

Distribution builds can include these unmodified upstream binaries:

- Xpra, GPL-2.0-or-later: https://github.com/Xpra-org/xpra
  The upstream bundle's license texts remain in
  `Xpra.app/Contents/Resources/share/xpra/COPYING`. Corresponding source is
  available from the linked project for the bundled version.
  BoxDeck applies a two-line Darwin cursor-size fix to the copied Python source
  so Retina cursor backing pixels are rendered at the correct logical size.
- cloudflared, Apache-2.0: https://github.com/cloudflare/cloudflared

BoxDeck does not expose an Xpra or boxserver network port. Both communicate
through the user's existing SSH configuration.
