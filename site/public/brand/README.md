# huta brand

A small shelter for two release slots. The roof stands for a carefully managed server; the two equal blocks are slots `a` and `b`. Their equal weight makes either a credible home for a release. The open space between them keeps the mark legible at small sizes. No motion or direction is implied: the owner chooses when to switch.

The lowercase wordmark is drawn as rounded geometric strokes with an even, typewriter-like rhythm. It sits comfortably beside visible Markdown punctuation without becoming another syntax symbol.

## Palette

| Colour | Hex | Role |
| --- | --- | --- |
| Pine | `#243E38` | Wordmark, icon tile, primary text |
| Sage | `#90AEA0` | Second slot, tile edge, quiet rules |
| Paper | `#F4F1E8` | Light canvas, roof, first slot |

Use pine text on paper and paper text on pine. Sage is an accent, not a small-text colour. The tile has a sage perimeter so its silhouette remains visible on light and dark surfaces. The logo changes its wordmark from pine to paper with the system dark-mode preference; when a page theme differs from the system preference, inline the SVG and set `.word` to the appropriate palette colour. The favicon reverses to a paper tile in dark mode, with both slots in pine for crisp tiny-size rendering. The social card always uses paper.

## Space and size

- Keep at least one slot width of clear space around the mark: 12 units per 64-unit tile (about 19% of its width).
- Keep the same clear space around the full logo, measured from the artwork. Its internal mark-to-word gap is already set.
- Minimum mark size: 16 × 16 CSS pixels. Prefer `favicon.svg` at 16–24 pixels; its shapes are tuned for that scale.
- Minimum horizontal logo height: 24 CSS pixels (about 92 pixels wide). Preferred height: 32–64 pixels. Preserve its aspect ratio.
- Never stretch, rotate, add shadows, or close the gap between the two slots.

## Files

- `logo.svg`: horizontal lockup, 244 × 64; mark plus hand-drawn wordmark paths.
- `mark.svg`: standalone 64 × 64 tile, transparent outside its rounded silhouette.
- `favicon.svg`: 16-unit icon with an embedded dark-mode media query.
- `og.svg`: 1200 × 630 social card with the promise and both domains. The wordmark is paths; supporting text uses a local generic monospace stack.
- `og.png`: 1200 × 630 raster export of the social card.
- `mark-512.png`: 512 × 512 transparent raster export of the mark.

SVGs have no remote resources, scripts, or embedded images. PNGs are rendered locally from the corresponding SVGs. Supporting social-card typography may vary by local monospace font; use the PNG for a fixed rendering.
