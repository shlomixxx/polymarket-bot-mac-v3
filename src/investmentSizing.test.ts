import { describe, it, expect } from "vitest";
import {
  investmentBaseUsd,
  effectiveInvestmentUsd,
  contractsFromInvestment,
  computeEffectiveMinContracts,
} from "./investmentSizing";

describe("investmentBaseUsd", () => {
  it("מצב קבוע: הבסיס הוא הסכום ב-$ בלי קשר ל-equity", () => {
    expect(
      investmentBaseUsd({ mode: "fixed", fixedUsd: 5, pctOfPortfolio: 5, equityUsd: 1000 }),
    ).toBe(5);
  });

  it("מצב אחוז: הבסיס = equity × אחוז / 100 (לא הסכום הקבוע)", () => {
    // הבאג שדווח: 5% מתיק של $1000 = $50, לא $5
    expect(
      investmentBaseUsd({ mode: "percent", fixedUsd: 5, pctOfPortfolio: 5, equityUsd: 1000 }),
    ).toBe(50);
  });

  it("מצב אחוז עם אחוז/equity לא תקינים → 0 (לא קורס)", () => {
    expect(
      investmentBaseUsd({ mode: "percent", fixedUsd: 5, pctOfPortfolio: NaN, equityUsd: 1000 }),
    ).toBe(0);
    expect(
      investmentBaseUsd({ mode: "percent", fixedUsd: 5, pctOfPortfolio: 5, equityUsd: NaN }),
    ).toBe(0);
    expect(
      investmentBaseUsd({ mode: "percent", fixedUsd: 5, pctOfPortfolio: -3, equityUsd: 1000 }),
    ).toBe(0);
  });
});

describe("effectiveInvestmentUsd", () => {
  it("בלי loss-recovery: הסכום האפקטיבי = הבסיס (המכפיל מתעלם)", () => {
    expect(
      effectiveInvestmentUsd({
        mode: "percent",
        fixedUsd: 5,
        pctOfPortfolio: 5,
        equityUsd: 1000,
        lossRecoveryEnabled: false,
        lossRecoveryMult: 3,
      }),
    ).toBe(50);
  });

  it("עם loss-recovery: הסכום האפקטיבי = בסיס × מכפיל", () => {
    expect(
      effectiveInvestmentUsd({
        mode: "percent",
        fixedUsd: 5,
        pctOfPortfolio: 5,
        equityUsd: 1000,
        lossRecoveryEnabled: true,
        lossRecoveryMult: 2,
      }),
    ).toBe(100);
  });

  it("מכפיל מתחת ל-1 או לא-תקין מתנהג כ-1 (לא מקטין את הגודל)", () => {
    expect(
      effectiveInvestmentUsd({
        mode: "fixed",
        fixedUsd: 10,
        pctOfPortfolio: 0,
        equityUsd: 0,
        lossRecoveryEnabled: true,
        lossRecoveryMult: 0.5,
      }),
    ).toBe(10);
    expect(
      effectiveInvestmentUsd({
        mode: "fixed",
        fixedUsd: 10,
        pctOfPortfolio: 0,
        equityUsd: 0,
        lossRecoveryEnabled: true,
        lossRecoveryMult: NaN,
      }),
    ).toBe(10);
  });
});

describe("contractsFromInvestment", () => {
  it("מספר חוזים = floor(סכום / מחיר), כשעומד ברצפה", () => {
    // $50 ב-50¢ = 100 חוזים
    expect(
      contractsFromInvestment({ investmentUsd: 50, entryCents: 50, minContracts: 5 }),
    ).toBe(100);
  });

  it("מתחת לרצפת המינימום → 0", () => {
    // $5 ב-50¢ = 10 חוזים, אבל מינ' 20 → 0
    expect(
      contractsFromInvestment({ investmentUsd: 5, entryCents: 50, minContracts: 20 }),
    ).toBe(0);
  });

  it("מחיר לא תקין → 0", () => {
    expect(
      contractsFromInvestment({ investmentUsd: 50, entryCents: 0, minContracts: 1 }),
    ).toBe(0);
  });
});

describe("computeEffectiveMinContracts", () => {
  it("בלי מינ' בורסה (שוק לא נטען) — ברירת מחדל בטוחה של 5", () => {
    expect(computeEffectiveMinContracts(5, undefined)).toBe(5);
    expect(computeEffectiveMinContracts(2, undefined)).toBe(5);
  });

  it("מינ' בורסה גבוה יותר גובר (עיגול כלפי מעלה)", () => {
    expect(computeEffectiveMinContracts(5, 7.2)).toBe(8);
  });

  it("מינ' בורסה נמוך יותר — נשארת רצפת המשתמש", () => {
    expect(computeEffectiveMinContracts(10, 3)).toBe(10);
  });
});

describe("רגרסיה: הבאג שדווח (מצב אחוז → תצוגת חוזים)", () => {
  it("5% מתיק $1000 ב-50¢ מציג 100 חוזים — לא 10 (כמו הסכום הקבוע)", () => {
    const usd = effectiveInvestmentUsd({
      mode: "percent",
      fixedUsd: 5,
      pctOfPortfolio: 5,
      equityUsd: 1000,
      lossRecoveryEnabled: false,
      lossRecoveryMult: 1,
    });
    const min = computeEffectiveMinContracts(5, undefined);
    expect(contractsFromInvestment({ investmentUsd: usd, entryCents: 50, minContracts: min })).toBe(
      100,
    );
  });
});
