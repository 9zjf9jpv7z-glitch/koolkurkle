# ATS-120R / Hi-Target Fileman: serial Import from PC and missing Z

Sunway ATS-120R (Hi-Target ZTS-120 family, NWI NTS03, same Fileman firmware).
Question: why RS-232 **Fileman → Import from PC** stores point names and horizontals but never elevation in the destination `.COO`, and what ASCII would make Z land.

## Short answer

Serial Import from PC is **not** Appendix A `SC`/`NEZ` and **not** Appendix B Z-commands. It is a **one-CRLF-line = one COO record** CSV parser. USB Import is the only path the manual documents as 3D (`PT, E, N, Z, CODE`).

The layouts already tried (`name,E,N,Z` and friends) put elevation in field 4. On this family’s usual **serial receive** layout, field 4 is a horizontal (or is discarded) and **Z is field 5**:

```text
name,code,E,N,Z
```

Empty code is fine; the comma must stay:

```text
ZTEST,,111.111,222.222,333.333
```

If that still writes Z = 0, this firmware’s serial importer is **2D-only**. Then no RS-232 payload stores elevation; USB Import (or keyboard entry) is the 3D path.

## What the three “formats” in the manual actually are

| Path | What it is | Z? |
| --- | --- | --- |
| **Fileman → Import from PC** | Undocumented ASCII. Manual says use the transfer software. Handshake: port open, Import, instrument `0x0D`, PC `ACK 0x06`, then CRLF lines at ~50 ms. | Not documented. Live tests: name + two horizontals. |
| **Fileman → Import from USB** | Text in `ts_prj\import` on the stick. Documented: `PT, E, N, Z, CODE`. Each line ends CRLF; last line must too. | Yes, field 4. |
| **Appendix A** | **Export** “Sunway” two-line records (`STA`/`SC`/`SD`… then `XYZ`/`NEZ`/`HVD`…). First-line “elevation” is **instrument/target height**, not Z. | Z is on the **second** line. |
| **Appendix B** | Live measure/control (`STX`+`TC`/`SA`/`RA`/`RD`/`DS`/`DT`). | Not file import. |

Sending Appendix A `SC` / `NEZ` over Import from PC stores the **words** `SC` and `NEZ` as point names and doubles `NO.:`. That parser does not understand record types.

Export “SSS” is Topcon GTS-7 for Topcon Link. Unrelated to Import from PC.

## Why `name,E,N,Z` leaves Z empty

Chinese total-station **receive** (Kolida KTS serial/SD, South CASS DAT, most office transfer tools) is:

```text
点名,编码,E,N,Z
```

USB on this firmware is the other common order: `PT, E, N, Z, CODE` (Z then code).

If serial Import from PC is the CASS receive parser, then:

| Sent | name | code | E | N | Z |
| --- | --- | --- | --- | --- | --- |
| `name,E,N,Z` | name | *(easting as code)* | N | Z | **missing → 0** |
| `name,E,N,Z,` | name | easting | N | Z | empty → 0 |
| `name,E,N,Z,P` | name | easting | N | Z | `P` → 0 |
| `name,N,E,Z` | name | northing | E | Z | **missing → 0** |

That matches “names and horizontals appear, Z always 0” **if the two stored horizontals were not checked against the file**. If they were, and E/N matched fields 2 and 3 exactly, the serial parser is instead 2D (`PT, E, N` or `PT, N, E`) and field 4 is code or ignored.

USB and serial are different code paths. Copying the USB column order onto RS-232 does not prove Z should land.

## Payload to try next (distinctive numbers)

Same handshake as the working Mac tests (115200 8N1, CoolTerm disconnected, ACK `0x06`, ~50 ms/line). One probe line is enough:

```text
ZTEST,,111.111,222.222,333.333
```

Also send, as separate imports or later lines:

```text
ZUSB,111.111,222.222,333.333,CODE
ZCASS,P,111.111,222.222,333.333
ZSPC 111.111 222.222 333.333
```

Read **N, E, Z, and CODE** on the point Info / stakeout recall screen, not the Fileman list.

| Result on `ZTEST,,111.111,222.222,333.333` | Meaning |
| --- | --- |
| E=111.111, N=222.222, **Z=333.333**, code empty | Serial is CASS `PT,CODE,E,N,Z`. That is the payload. |
| E=111.111, N=222.222, Z=0, code maybe `333.333` | Serial is 2D `PT,E,N[,CODE]`. No RS-232 Z. |
| N=111.111, E=222.222, Z=333.333 | CASS with N/E swapped; use `name,code,N,E,Z`. |
| List shows two numbers, Z blank on Edit | See traps below — confirm on N/E/Z recall. |

Do not send a two-line `SC`/`NEZ` (or `XYZ`) pair. Each line is a record.

No header is required: `NO.:` already counts lines. `NEZ`/`ENZ` under Surveying config is **display/input prompt order**, not an import switch. Appendix B and Coord.Z (station height from known points) are not file import.

## Display traps (easy to call a stored Z “empty”)

1. COO **list** views on this family often print `name: horiz1 horiz2` only (Hi-Target ZTS-120 manual: `A001:100.0 100.0`). Z is on the record detail / STA / stakeout recall.
2. Fileman Edit: “only point name, code and **height** can be edited.” That height is **target height** (Appendix A first-line field), not Z.
3. After import, call the point as station or stakeout and read the N/E/Z page.

## Official PC software

Import from PC is written for Hi-Target / Sunway **Data Transfer** / **PC-Port2** / **PC-IO**, not a terminal. Those tools convert office CSV into whatever the serial parser expects (very likely CASS `name,code,E,N,Z`). The instrument does not speak Appendix A on the way in.

## Practical path if serial stays 2D

- USB Import (`ts_prj\import`, `PT,E,N,Z,CODE`) is the documented 3D import.
- Mini-USB “instrument as disk” still needs that USB Import conversion; internal `.COO` is binary.
- Keyboard “Entering coordinates” includes Z.
- Measuring into the COO (Save / ★) writes a real Z.
