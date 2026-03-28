/** מזהה SVG בטוח לגרדיאנט (מניעת כפילויות בין סשנים) */
export function safeSvgIdPart(s: string): string {
  return String(s).replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 80) || "s";
}
