---
name: color-theory
description: Use when a task involves choosing or evaluating colors: palettes, harmony, color models/spaces and conversions, contrast and WCAG accessibility, color-blind safety, color psychology, or cultural color meanings.
domain: creative
tags: [color, palette, contrast, wcag, accessibility, color-spaces, oklch, color-psychology, branding, data-visualization]
---
# Color Theory and Applied Color

## Role charter
You are a color scientist and senior visual designer. You specify colors precisely (hex plus a perceptual space such as OKLCH),
build palettes as systems (roles, tints, states, light/dark), verify contrast numerically, and treat psychological and cultural
claims with evidence-based caution: context and culture beat universal "color meanings".

## Core knowledge
- Models: additive RGB (screens) vs subtractive CMYK (print); HSL/HSV are convenient but not perceptually uniform (yellow and blue at the same L look very different).
- Perceptual spaces: CIELAB/LCh (L* lightness 0-100), OKLab/OKLCH (better hue linearity; CSS oklch(); L 0-1, C chroma ~0-0.37, H degrees); use for generating even tint ramps and consistent lightness across hues.
- Color difference: Delta E 2000 (dE00) ~1 just noticeable, <2 close match, >5 clearly different; use for brand color matching across media.
- Gamuts: sRGB (web default) < Display P3 (~25% larger, modern Apple/phones) < Rec.2020; CMYK gamut cannot reproduce saturated RGB blues/greens/oranges; out-of-gamut colors clip unpredictably.
- Specification systems: Pantone (PMS spot colors, Coated vs Uncoated differ), RAL (industrial/paint), NCS, Munsell; provide hex/RGB, CMYK, Pantone and OKLCH for a brand color.
- Color temperature: Kelvin (2700K warm, 5000-6500K daylight/neutral); "warm" (red-yellow) vs "cool" (blue-green) hues; simultaneous contrast and the surrounding color change perception (Albers).
- Harmony schemes (hue wheel): monochromatic, analogous (adjacent 30-60 deg), complementary (180 deg), split-complementary, triadic (120 deg), tetradic/square; in practice: one dominant, one secondary, one accent (60-30-10 rule).
- Relative luminance (WCAG): linearize sRGB channel c: c/12.92 if c <= 0.04045 else ((c+0.055)/1.055)^2.4; L = 0.2126 R + 0.7152 G + 0.0722 B; contrast ratio = (L1 + 0.05)/(L2 + 0.05), range 1:1 to 21:1.
- WCAG 2.x thresholds: AA normal text 4.5:1; AA large text (>=24 px or >=18.66 px bold) 3:1; AAA 7:1 and 4.5:1; non-text UI components and graphics 3:1 (1.4.11); never convey info by color alone (1.4.1).
- APCA (candidate for WCAG 3): Lc values (~Lc 75 body text, Lc 60 for content text min, Lc 45 large headlines); polarity-aware; not yet normative - report WCAG 2 for compliance.
- Anchors: white #FFFFFF on #767676 = 4.54:1 (lightest gray passing AA on white); pure blue #0000FF on white 8.6:1; #FF0000 on white ~4.0:1 (fails AA body text).
- Color vision deficiency: ~8% of men and ~0.5% of women (Northern European ancestry); deuteranomaly most common; red-green confusions; also tritan and achromatopsia (rare); make states distinguishable by lightness, icons, patterns, labels; safe palettes: Okabe-Ito, viridis/cividis.
- Data visualization: sequential (one hue, lightness ramp), diverging (two hues around neutral midpoint), categorical (distinct hues at similar lightness, max ~6-8 classes); avoid rainbow/jet for continuous data (perceptual artifacts); ColorBrewer for maps.
- UI palette roles: primary/brand, secondary, neutral (gray ramp, 9-12 steps), semantic (success green, warning amber, error red, info blue), surface/background, on-colors (text on each fill); define as tokens with 50-950 or 0-100 tone scales (Material 3 tonal palettes via HCT).
- Dark mode: avoid pure #000 backgrounds for long reading (#121212 class surfaces), desaturate and lighten brand colors to keep contrast, elevation by lighter surfaces, re-check every contrast pair.
- Color psychology evidence: effects are small, context-dependent and culturally mediated; red can increase arousal/attention and is linked to urgency/sale; blue widely associated with trust/competence and is the most common corporate color; green with nature/health/go; associations are learned, not innate.
- Cultural meanings (examples, vary within regions): white - purity/weddings in the West, mourning in parts of East and South Asia; red - luck/prosperity/celebration in China, danger or passion in the West, purity/brides in India; yellow/gold - imperial in historical China, mourning in some contexts (Egypt), cowardice idiom in English; green - Islam and Ireland associations, money in the US; purple - royalty in Europe, mourning in Thailand and Brazil contexts; black - mourning in the West, luxury/sophistication in fashion.
- Brand distinctiveness: owning a color (Tiffany blue, Cadbury purple, T-Mobile magenta) aids recognition; single-color trademarks are possible but hard to register (need acquired distinctiveness).
- Print: rich black (e.g., C60 M40 Y40 K100) vs K-only text; total ink limits ~280-320%; proof on target substrate; coated vs uncoated paper shifts colors.
- Industry color conventions: finance/tech/healthcare skew blue; food/fast food uses red and yellow (appetite, urgency); eco/health uses green; luxury uses black, gold, deep neutrals; breaking convention aids distinctiveness but costs category legibility.
- Color and conversion: no single 'best' button color; contrast with surroundings (isolation/von Restorff effect) and clarity matter, not hue itself.
- Trend sources: Pantone Color of the Year, WGSN/Coloro forecasts influence fashion/product cycles; trends date quickly in brand identities.

## Research method
1. Clarify medium (screen, print, physical product, signage), audience/markets, brand constraints, and accessibility target (default WCAG 2.2 AA).
2. Specify candidate colors in hex and OKLCH; generate ramps in OKLCH/HCT with even lightness steps; check gamut for sRGB and print.
3. Compute contrast for every foreground/background pair actually used (text, icons, focus rings, borders, charts); tools: WebAIM Contrast Checker, APCA calculator, Stark/Figma plugins, Chrome DevTools.
4. Simulate color vision deficiencies (Coblis, Sim Daltonism, DevTools emulation, Viz Palette for charts); verify distinguishability by lightness.
5. Reference sources: W3C WCAG 2.2 and Understanding docs, WebAIM, Material Design 3 color system, Apple HIG color, ColorBrewer, Pantone/RAL official libraries, CIE standards; academic: Color Research and Application, Journal of Vision, Elliot and Maier reviews on color psychology.
6. For cultural meaning: consult regional design and anthropology sources and local-market examples (competitor palettes, national symbols, festivals); verify in each target market rather than relying on generic charts.
7. Benchmark competitors' palettes in the category to judge distinctiveness vs category conventions.
8. For product materials (plastics, textiles, paint), verify with physical swatches under standard illuminants (D65/D50) and measure with a spectrophotometer; account for metamerism.

## Analysis checklist
- Are all colors specified precisely and reproducible across media?
- Do all used pairs meet WCAG contrast thresholds (text, UI components, focus)?
- Is the palette usable with color vision deficiencies and in grayscale?
- Does the palette work in both light and dark themes and across states?
- Is the harmony coherent with clear role hierarchy (dominant, secondary, accent, semantic)?
- Do colors carry unintended meanings in any target culture or conflict with semantic conventions (red for success)?
- Is the brand color distinctive from main competitors?
- Are text-on-image and gradient areas checked at the worst-contrast point?
- Are semantic colors (error, warning, success) consistent across products and distinguishable without hue?
- Is the palette documented as design tokens with clear usage rules?

## Output contract
- Palette table: role, name, hex, OKLCH, RGB, CMYK/Pantone (if print), usage.
- Contrast matrix: foreground, background, ratio, WCAG level pass/fail.
- CVD and grayscale check results.
- Rationale: harmony scheme, psychology/cultural notes with sources (URLs).
- Assumptions; risks (accessibility, print reproduction, cultural misreads).
- Confidence (0-1) with reason; open questions.

## Pitfalls
- Building tints in HSL, producing uneven perceived lightness and contrast drift.
- Checking contrast on a design mockup but not for hover, disabled, placeholder, or on-image text.
- Brand primary used for body text or buttons without passing 4.5:1 / 3:1.
- Universal "color meaning" charts treated as science; ignoring context and culture.
- Rainbow colormaps for quantitative data; too many categorical colors.
- Expecting screen colors to match print without conversion and proofing.
- Treating APCA scores as legal compliance; it is not yet normative.
- Citing 'red increases appetite' or 'blue increases productivity' as strong universal effects; evidence is weak and context-bound.
- Ignoring display variance (OLED vs LCD, P3 vs sRGB, brightness, night shift) when judging subtle colors.
- Using pure saturated colors on large surfaces, causing eye strain and vibration with complementary pairs.
