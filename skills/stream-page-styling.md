# Stream Page Styling Skill

Guidelines for optimizing stream/broadcast page layouts in this project.

## Layout Density Rules (Broadcast/Fit Mode)

- Root padding: `4px 8px 6px` (minimal chrome)
- Section margins: max `6px` between major blocks
- Stat grid gaps: `8px` in fit mode
- Price pill padding: `12px 18px`, font `34px` for cents
- Pulse orb: `130px` diameter in fit mode (down from 164px)
- Chart height: `240px` in fit mode

## CSS Variable System

All colors use CSS custom properties defined in `src/index.css`:
- `--bg`, `--bg-elevated`, `--card` for surfaces
- `--text`, `--text-secondary`, `--muted` for text hierarchy
- `--up` / `--down` for financial sentiment (green/red)
- `--border`, `--border-strong` for subtle dividers
- `--accent-bright` for highlights

## Gold Theme (Spectator/Pro Pages)

- Primary accent: `#fbbf24` (amber-400)
- Light gold text: `#fde68a`
- Gold borders: `rgba(251, 191, 36, 0.4)`
- Gold shadows: `rgba(251, 191, 36, 0.12)`
- Background gradients use `rgba(251, 191, 36, 0.07)` radial overlays

## Broadcast Fit Mode

- `BroadcastFit` component scales content to `100dvh` via CSS transform
- Content must NOT overflow — everything fits in one viewport
- Footer is hidden via `.stream-broadcast-fit footer { display: none; }`
- Important elements (QR codes) must be placed in `<div>`, not `<footer>`

## Inline Styles vs CSS Classes

This project uses primarily inline styles in React components. CSS classes are used for:
- Animations (`@keyframes` in `<style>` blocks inside components)
- Pseudo-elements (`:before`, `:after`)
- Global resets and typography (`index.css`)
