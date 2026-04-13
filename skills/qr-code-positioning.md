# QR Code Positioning Skill

Guidelines for placing QR codes in broadcast-safe stream pages.

## Library: `qrcode.react`

Already installed in this project (v4.2.0). Usage:

```tsx
import { QRCodeSVG } from "qrcode.react";

<QRCodeSVG
  value="https://t.me/roller000"
  size={48}
  bgColor="#ffffff"
  fgColor="#0f172a"
  level="M"
  style={{ borderRadius: 6, flexShrink: 0 }}
  role="img"
  aria-label="QR code - Telegram @roller000"
/>
```

## Broadcast-Safe Placement

**Problem**: In broadcast/fit mode, `<footer>` elements are hidden by CSS rule `.stream-broadcast-fit footer { display: none; }`.

**Solution**: Place QR codes in `<div>` elements within the main content flow, NOT inside `<footer>`.

## Recommended QR Strip Pattern

```tsx
<div style={{
  display: "flex",
  alignItems: "center",
  gap: 14,
  padding: "8px 14px",
  marginTop: 8,
  borderRadius: 10,
  border: "1px solid rgba(251, 191, 36, 0.25)",
  background: "linear-gradient(135deg, rgba(15, 23, 42, 0.96), rgba(30, 41, 59, 0.45))",
  boxShadow: "0 0 18px rgba(251, 191, 36, 0.06)",
}}>
  <QRCodeSVG value="https://t.me/roller000" size={48} ... />
  <span style={{ color: "#fbbf24", fontWeight: 700 }}>@roller000</span>
</div>
```

## Sizing Guidelines

- **48px**: Compact broadcast mode (scannable on 1080p+)
- **80px**: Standard non-broadcast pages
- **120px**: Large/prominent placement
- Error correction level: "M" (medium) is sufficient for clean URLs
- Always use white background with dark foreground for contrast on dark themes
