# Sample lists

Both files use fictional farms and coordinates near Kiambu, Kenya. They are synthetic teaching data, not surveyed farm boundaries or evidence about real producers. The demo's `SYN-SWAPPED-005` point is deliberately outside Kenya: `[longitude, latitude]` is `[-1.100000, 100.000000]`, so latitude is outside ±90 while swapping the pair would make both values numerically valid. A coordinate swap near the equator can remain numerically legal and cannot be detected by this rule alone.

`demo.geojson` has 23 features, indexed from zero in file order: 13 each carry one planted mistake and 10 are valid, so the demo looks like a real list (mostly fine, with some problems). Expected rule coverage:

| Index | Farm ID | Expected reason code(s) |
|---:|---|---|
| 0 | KMB-001 | `DUPLICATE_PLOT` (same point as feature 10) |
| 1 | missing | `MISSING_FARM_ID` |
| 2 | KMB-003 | `INVALID_GEOMETRY_TYPE` |
| 3 | KMB-004 | `LOW_PRECISION` |
| 4 | SYN-SWAPPED-005 | `LIKELY_SWAPPED_COORDS` |
| 5 | KMB-006 | `POINT_OVER_4HA` |
| 6 | KMB-007 | `POLYGON_NOT_CLOSED` |
| 7 | KMB-008 | `POLYGON_TOO_FEW_VERTICES` (three distinct vertices; the closing coordinate is not counted) |
| 8 | KMB-009 | `POLYGON_SELF_INTERSECTS` |
| 9 | KMB-010 | `AREA_MISMATCH` |
| 10 | KMB-011 | `DUPLICATE_PLOT` (same point as feature 0) |
| 11 | KMB-012 | `OVERLAPPING_PLOTS` (overlaps feature 12) |
| 12 | KMB-013 | `OVERLAPPING_PLOTS` (overlaps feature 11) |
| 13 | KMB-014 | none |
| 14 | KMB-015 | none |
| 15–19 | KMB-016 to KMB-020 | none (irregular 5–6 corner farms, 1.3–3.3 ha, declared areas match their measured areas) |
| 20–22 | KMB-021 to KMB-023 | none (points for farms of 4 ha or less) |

The three-distinct-vertex triangle at index 7 is accepted by PostGIS `ST_IsValid`; the app's requested minimum is four distinct vertices, so `POLYGON_TOO_FEW_VERTICES` is an independent rule. `ST_IsValid` checks polygon topology, and `ST_IsValidReason` supplies the readable cause when that check fails. The bowtie at index 8 reports `Self-intersection[36.815 -1.111]`.

`ST_Area(geom::geography)` measures area on the earth in square metres; dividing by 10,000 converts it to hectares. The index 9 polygon measures 0.997 ha against its declared 10 ha, so it intentionally exceeds the 20% mismatch limit. Each rectangle at indexes 11–13 measures 4.923 ha against 4.9 ha. For overlaps, `ST_Intersects` removes disjoint polygon pairs before `ST_Intersection` builds their shared shape. The geography overload of `ST_Intersection` chooses a suitable local projection internally and transforms the result back to geography; `ST_Area` then measures it in square metres. Dividing by the smaller polygon's geography area gives the overlap percentage. Indexes 11 and 12 share 50.00% of the smaller area, above the 1% limit. The other pairs do not overlap. Geography is used for area and overlap because it measures on the curved earth in metres, while the source coordinates are angular degrees.

For `LOW_PRECISION`, the loader keeps each number's source token. The rule counts effective decimal places after applying any exponent: `1.234567e2` represents four places and fails, while `1e-6` represents six and passes. Trailing zeros count. This measures the precision written in the file, not the real-world accuracy of a coordinate.

`valid.geojson` contains three clean examples and should produce no reason codes.
