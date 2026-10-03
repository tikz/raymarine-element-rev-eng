# Raymarine Element 7 reverse engineering

Inside the Raymarine **Element 7 S**: how it runs, what it exposes, and how to read its navigation data and control its display. This reverse-engineering guide turns hands-on experiments and firmware analysis from the `raymarine-element` research project into practical protocol documentation for building your own integrations.

The investigated baseline is product **E70531**, application **3.13.332**, bundle **3.13.76**, platform **3.13.1**. Product codes and firmware versions describe compatibility; individual device identifiers and installation details are deliberately excluded. These findings have not been validated across all Element models or firmware releases.

## Contents

| Section | Contents |
| --- | --- |
| [Python PoCs](#python-pocs) | Runnable navigation, RayDB and SFTP examples; offline tests |
| [Architecture and compatibility](#architecture-and-compatibility) | Android platform, application services, GNSS implementation, established limits |
| [Network services](#network-service-inventory) | Endpoint inventory, roles, discovery, unverified assignments |
| [Native navigation protocol](#native-navigation-database-tcp-2051) | TCP 2051 framing, transactions, GPS decoding, freshness and no-fix behavior |
| [Measurement catalog](#measurement-catalog-and-units) | Navigation item IDs, types, units, satellite tables and supply voltage |
| [RayDB protocol](#raydb-settings-and-diagnostics-tcp-23333) | TCP 23333 framing, GET, typed values, diagnostics and settings |
| [SFTP and storage](#sftp-access-and-storage) | Connection prerequisites, account restrictions, SD-card transfers |
| [Brightness control](#remote-lcd-brightness-control) | Sysfs interface, firmware percentage curve, Python reference and readback |
| [Evidence and open questions](#evidence-confidence-and-unresolved-work) | Claim provenance, confidence, unresolved work |

## Main findings

- Direct TCP **2051** `GetPreferredItem` queries returned GPS position/status, SOG/COG, depth, water temperature, altitude and stored statistics. The observed read path required no SSH tunnel or application authentication exchange.
- TCP **23333** exposes RayDB settings and diagnostics. Navigation measurements were absent from the investigated RayDB capture; use the native database for those measurements.
- Display supply voltage is readable through RayDB or the `secondary/vbatt` sysfs attribute over authenticated SFTP.
- Authenticated SFTP can transfer SD-card files and write the LCD brightness attribute. Shell execution is restricted.
- Remote theme values were changed and restored through RayDB, with independent readback. Visible UI effect and reboot persistence were not established.

These are reverse-engineered observations, not a vendor-supported API contract. Each section separates observed behavior, firmware analysis and unresolved interpretation. Transport success does not establish sensor validity or freshness.

## Repository scope

This repository contains reviewed prose, runnable Python PoCs and synthetic offline tests. It excludes raw captures, firmware dumps, credentials, SSH host keys, device serials, MAC addresses, installation addresses, vessel details, routes, tracks and actual coordinates. Examples use `ELEMENT_HOST`, `PRODUCT_CODE SERIAL`, and `SOURCE_ID` placeholders. Supply your own values locally; keep them out of commits.

The private research history is not imported. No device interaction is required to read or maintain this documentation.

## Python PoCs

The [poc/](poc/) directory contains one-shot Python 3.10+ command-line examples. They use the standard library; SFTP additionally requires the OpenSSH `sftp` executable. Keep the directory together because the scripts share [common.py](poc/common.py). Run commands from the repository root. No pip installation is required.

| Script | Operations |
| --- | --- |
| [navigation.py](poc/navigation.py) | TCP 2051 preferred navigation-item reads and decoded JSON |
| [raydb_get.py](poc/raydb_get.py) | TCP 23333 HELLO followed by one exact-key GET |
| [sftp_device.py](poc/sftp_device.py) | Supply voltage, brightness bounds/readback, explicit brightness writes |

### Navigation reads

Set `ELEMENT_HOST` locally to the verified device address, then run:

```sh
python3 poc/navigation.py --host "$ELEMENT_HOST"
python3 poc/navigation.py --host "$ELEMENT_HOST" \
  --items position quality hdop satellites sog cog depth water_temperature altitude
```

The default queries position, quality, HDOP and UTC date/time. Requesting position automatically adds quality. Each item uses a separate connection, a default three-second connect timeout and three-second response deadline, with 250 ms between queries. `--timeout` changes those bounds; `--port` overrides the default 2051.

Results include per-item state, collection time, units and transport timeout/type. Source identifier bytes are checked for framing but not printed. `fix_state` is `no_fix`, `unavailable`, or `fix_indicated`; the last means readable position plus nonzero quality, not proof of sensor freshness. Coordinates may remain in output when quality reports no fix—consumers must check the status. Missing replies are never converted to zero coordinates.

The PoC validates the observed status/start/chunk/end transaction order, sequence/index, matching transaction IDs, type/size and metadata lengths. It assembles multiple chunks with a 64 KiB response budget. Other transaction orders and schema variants are rejected. Reads are sequential; the output is not an atomic snapshot or a validated combined date/time.

### RayDB GET

Provide an exact key, replacing the identity placeholder with your own local value:

```sh
python3 poc/raydb_get.py --host "$ELEMENT_HOST" \
  --path 'diag/mfd/PRODUCT_CODE SERIAL/product_info/supply_voltage'
```

This sends only HELLO and GET, waits for a matching UPDATE, and exits. It supports Boolean, unsigned/signed 32-bit integer, float, double and UTF-8 string variants. Unsupported variants and malformed frames produce errors; NaN/infinity become JSON `null` with state `nonfinite`. The default response deadline is four seconds with a 64 KiB budget. `--timeout` and `--port` are configurable. No automatic key enumeration, subscriptions, SET operations or retry loop are included.

### SFTP reads and brightness writes

An enrolled public key and already verified host-key file are prerequisites. Set `ELEMENT_KEY` to the corresponding private-key path. The following examples enable the legacy RSA compatibility required by the investigated baseline; omit `--legacy-rsa` when unnecessary.

```sh
python3 poc/sftp_device.py --host "$ELEMENT_HOST" --key "$ELEMENT_KEY" \
  --known-hosts local/known_hosts --legacy-rsa voltage

python3 poc/sftp_device.py --host "$ELEMENT_HOST" --key "$ELEMENT_KEY" \
  --known-hosts local/known_hosts --legacy-rsa brightness

# Changes the display and leaves it at 50% firmware-curve equivalent.
python3 poc/sftp_device.py --host "$ELEMENT_HOST" --key "$ELEMENT_KEY" \
  --known-hosts local/known_hosts --legacy-rsa set-brightness --percent 50
```

`set-brightness` reads bounds and the previous raw level, prints them to stderr before writing, uploads the decimal value, and verifies `actual_brightness`. It performs no automatic restoration. Use `set-brightness --raw PREVIOUS_VALUE` to restore a previously recorded level. A failed upload may still have changed hardware state; the script reports that uncertainty and does not retry. Host-key checking stays enabled; no shell command execution or key enrollment is attempted. The default timeout is eight seconds per SFTP batch, not for the entire multi-batch operation.

### Output and validation

The scripts write JSON to stdout, errors to stderr, and exit nonzero for failed/unavailable reads or failed control verification. SFTP uses temporary local files and removes them on normal completion. Nothing creates persistent captures automatically. Live output can contain coordinates, device-identifying RayDB paths and values; keep saved output in ignored `local/` storage and review it before sharing.

These new scripts have **offline validation only**. The underlying operations were demonstrated in the private research; the packaged PoCs have not been tested against hardware. The synthetic tests cover split frames, multi-chunk replies, truncation, length limits, transaction mismatch, missing fields, signedness, nonfinite values, brightness conversion and SFTP write/readback/failure handling.

```sh
python3 -m unittest discover -s tests -v
```

Tests do not contact a device. For commands and available field names, run any script with `--help`. Long-running collection, natural loss/recovery, receiver control, general settings mutation and reconnect policies remain outside these PoCs.

## Architecture and compatibility

### Investigated platform

| Layer | Evidence from the investigated firmware |
| --- | --- |
| Product | Element 7 S, product code E70531 |
| Application / bundle / platform | 3.13.332 / 3.13.76 / 3.13.1 |
| Operating system | Android 7.1.2, SDK 25 |
| Build target | Qualcomm msm8937, arm64 |
| Kernel | 3.18.31-perf in prior research notes |
| Application internals | Native libraries implementing LNet, RayDB, settings, GPS and display management |

These are findings for the investigated baseline, not a hardware specification for every Element 7 variant. In particular, S and HV variants must not be treated as interchangeable without validation.

### Data paths

Internal GPS publication feeds the native navigation database. Its `GetPreferredItem` operation selects a preferred record for an index and returns typed payload plus source metadata. The network consumer sees this database through TCP 2051. Local sounder records supplied depth and water temperature. External-source selection and NMEA 2000 forwarding were not established.

RayDB is a separate settings/status/diagnostics database. Its network adapter on TCP 23333 connects internally to loopback TCP 49111. The two endpoints use different framing. Absence of navigation values from RayDB does not establish absence of a GPS fix or native navigation data.

Android sysfs drivers expose display brightness and input voltage. The restricted SFTP account can access selected attributes without shell execution. Application settings and hardware state can diverge: a direct brightness write changed driver readback without changing the saved application brightness value.

### GNSS implementation

Static loader analysis shows Android selecting `hw_get_module_by_class("gps", "u-blox", ...)` when the carrier `ublox_fitted` flag is ASCII `1`. The fitted flag and installed 64-bit HAL corroborate a u-blox implementation. The driver build was **18.06.01 (51699c6809-dirty)**.

Configuration refers to `/dev/ttyHSL0`, 115200 baud and initial 9600 baud. `RECEIVER_GENERATION = 7` is configuration, not independently verified silicon identity. The receiver serial stream was not read for this research. Installed driver version and receiver firmware version are different things; the exact receiver model and independent firmware remain unknown.

The HAL does not expose the investigated `gps-debug` interface. Its `gnss_raymarine` extension has SBAS enablement getter/setter functions, not a receiver-version getter. UDP registration/relay helper code exists, but no active GNSS UDP endpoint was established. Those helpers are not a verified alternative data API.

### Product identity versus receiver identity

Native index `0x97`, type `0x79`, carries two little-endian u16 values. For this product/application baseline the non-unique pair is **357, 313**: product ID 357 maps to E70531 / Element 7 S; 313 is the compact application code associated with 3.13.332. It does not include build 332 or identify GNSS-chip firmware. A general version encoding rule has not been established.

Source metadata also contains an encoded device serial. Its value is deliberately omitted here. Treat those source bytes as identifying data even when they are presented as integers rather than printed serial numbers.

### Boundaries

No general remote shell, root access, raw NMEA gateway, usable screen mirror, remote route-edit API or automatic GPX import was established. File upload capability alone does not establish application import. Natural GPS loss/recovery, source switching, midnight rollover and long-running reconnect behavior remain unresolved.

Provenance: private EXP-008 system inventory, EXP-012 script/binary inspection, EXP-013, EXP-022/023 navigation, EXP-030 product identity and EXP-031–033 GNSS investigations. See [evidence policy](#evidence-confidence-and-unresolved-work).

## Network service inventory

The following records describe the investigated firmware. Listener availability can change with configuration or restart. Some LNet service ports are allocated dynamically; a familiar port number alone does not prove a service identity.

| Endpoint | Established role or observation | Limit |
| --- | --- | --- |
| TCP 22 | SSH, restricted `media_rw` SFTP; authenticated forwarding observed | No interactive shell; enrollment persistence unresolved |
| TCP 2051 | Native LNet database; successful preferred navigation-item reads | See [protocol](#native-navigation-database-tcp-2051); broader API not mapped |
| TCP 2055 | LNet navigation status; 54-byte frame, message `0x00070007`, matched serializer | Named field meanings incomplete; not raw NMEA |
| TCP 2053/2054 | Reachable and silent in probes | Waypoint/track candidates; assignments unverified |
| TCP 23333 | RayDB remote adapter; HELLO/GET and targeted setting mutation verified | Distinct from navigation database |
| TCP 49111, loopback | Internal RayDB, diagnostic GET through authenticated forwarding | Different framing from 23333 |
| TCP 6668 / UDP 6667 | Strong static attribution to LNet diagnostics | Runtime metadata confirmation incomplete |
| TCP 8080 | HTTP root returned HTTP/1.0 404; startup launches BusyBox httpd | No root web UI established; other resources not excluded |
| TCP 8182 | Mobile-access messages; public-key enrollment previously demonstrated | Not documented here as a reusable enrollment client |
| UDP 5800/5801 | LNet discovery/listeners | Discovery response not required for successful direct 2051 reads |
| UDP 2050/2052/2056/3220 | Sockets recorded | Individual roles unresolved |
| UDP 5353 | mDNS; historical RayDB/control/RTSP advertisements | Advertised service does not prove usable endpoint |
| UDP 1900 | Listener consistent with IR-camera discovery bind code | Runtime ownership/camera presence unverified |
| TCP/UDP 53 | Loopback/P2P listener observations | No general LAN DNS service established |
| TCP 8554/50000 | RTSP/control advertised historically but closed when checked | No usable mirroring/control established |

### Discovery and address selection

Compiled LNet primary-address selection accepts loopback or addresses in `10.0.0.0/11`; investigation found that wireless addresses outside that range could be excluded. Loopback selection was an inference from code and interface state, not a process-memory observation. Do not infer missing navigation data from absent Wi-Fi discovery traffic.

A separate packed GPS-status datagram implementation is 640 bytes. It is distinct from native TCP database replies, and a usable live multicast endpoint was not established.

### Consumer choice

Use direct [TCP 2051](#native-navigation-database-tcp-2051) for the verified navigation records, [RayDB](#raydb-settings-and-diagnostics-tcp-23333) for identified diagnostic/settings paths, and [authenticated SFTP](#sftp-access-and-storage) for permitted file/sysfs access. Determine the host locally and confirm it is the intended device; do not hard-code a research installation's address.

Provenance: private EXP-013 and endpoint investigations EXP-015–021, plus EXP-029 theme mutation. No listener was probed while preparing these public documents.

## Native navigation database: TCP 2051

Direct TCP `GetPreferredItem` reads returned navigation data on the [documented baseline](#architecture-and-compatibility). No SSH tunnel, application HELLO or authentication payload was required for the observed query. This does not establish availability on every firmware or network configuration.

Connect to your locally determined `ELEMENT_HOST` on port 2051. The examples below serialize protocol constants only and contain no captured device values.

### Request format

All integers below are little-endian. Send four u32 words:

```text
[12] [0x00100105] [sequence] [database_index]
```

The first word is the body length, excluding that four-byte prefix. `0x00100105` is `GetPreferredItem`, a read operation traced in both client and server firmware. A position request with sequence 1 is exactly:

```text
0c 00 00 00  05 01 10 00  01 00 00 00  44 00 00 00
```

Equivalent local serialization: `struct.pack('<IIII', 12, 0x100105, sequence, index)`.

No application HELLO, authentication payload, or subscription was needed on this connection. This is the observed read protocol, not a complete service API; do not substitute RayDB frames or send guessed control messages.

| Field | Index | Returned type | Length | Decoding / meaning |
| --- | --- | --- | --- | --- |
| Position | `0x44` | `0x3c` | 8 | `<ii`, latitude then longitude; divide by 10,000,000 |
| GPS quality | `0x7d` | `0x60` | 1 | `0` means no fix in inspected firmware; nonzero codes need interpretation |
| HDOP | `0x7f` | `0x62` | 2 | Signed `<h` / 100; dimensionless, not metres of accuracy |
| Satellites in use | `0x77` | `0x5b` | 32 | 32 satellite-ID bytes; count nonzero entries |
| Fix-mode/smoothing setup | `0x94` | `0x76` | 2 | Configuration, not independent current 2D/3D fix proof |
| UTC time | `0xee` | `0x04` | 4 | Unsigned `<I` / 10,000 seconds since midnight |
| UTC date | `0xef` | `0x03` | 2 | Unsigned `<H`; days since 1970-01-01, verified in producer and consumer |

The HDOP and time divisors were read directly from ELF constants used by `GPSFacade`. The position-unit function names explicitly state `0_0000001_DEG`. Private EXP-013 retains the disassembly basis; EXP-029 subsequently verifies the date epoch.

### Response framing and validation

TCP may split a frame across receives or combine multiple frames. Reassemble using the u32 body-length prefix; never assume one `recv()` is one message.

Successful captured queries each returned these four bodies:

| Body command | Layout after command u32 |
| --- | --- |
| `0x100003` | sequence u32, index u32, status u16, storage u16; captured status `0` means retrieval succeeded |
| `0x100200` | sequence u32, transaction ID (8 bytes), serialization type u32 |
| `0x100201` | sequence u32, chunk length u32, serialized chunk |
| `0x100202` | sequence u32, transaction ID (8 bytes), matching the start |

A successful **query** status is not a GPS **fix** status. Read the quality item separately.

Observed serialized chunk layout:

| Offset within chunk | Field |
| --- | --- |
| 0 | Database index u32 |
| 4 | Type u16 |
| 6 | Payload length u32 (unaligned) |
| 10 | Reserved u16 |
| 12 | Payload bytes |
| 12 + payload length | Transport metadata, retained but not fully interpreted |

Check response sequence, requested index, item type/length, matching start/end transaction IDs and complete framing. Do not decode an absent/failed item as zero coordinates. These small records fit a single data chunk; a general client needs multi-chunk handling. The experiment used a three-second connect timeout, three-second per-query receive deadline, a 65,536-byte receive cap, sequential requests and 250 ms pauses.


### Transport metadata

Immediately after the item payload, the inspected serializer emits:

```text
u32 transport_type
u32 timeout_seconds
u16 source_data_length
u16 reserved_zero
u8  source_data[source_data_length]
```

Types identified in firmware include **19 = Internal**, **3 = STHS**, and **10 = Calculated**. For the observed internal GPS form, source data comprises an instance byte followed by a little-endian u32 encoded serial. The observed STHS form contains the u32 serial. Do not apply these layouts blindly to other transport types. Serial values are private identifiers and must be removed from published fixtures.

The serializer omits last-update time. `timeout_seconds` is a database policy field, not the present age of a measurement. A locally recorded receive time measures when the client collected the record, not when the sensor generated it.

### Validity and no-fix handling

Wire GET status `0` means retrieval succeeded. Status `1` collapses non-success results and cannot distinguish missing, expired and other query failures. Neither value describes GPS fix quality.

Static firmware analysis found an internal GPS timeout branch testing **2000 ms**. Its no-fix routine writes quality zero, clears ordinary/differential in-use lists and deletes HDOP, accuracy, altitude, SOG/COG and date/time records. Position is not deleted by that routine. Other lifecycle paths may remove it; indefinite retention is not established. This is static analysis, not an observed natural loss/recovery experiment.

A consumer should:

1. Validate complete frames, sequence/index, item type/size and transaction IDs before interpreting a value.
2. Require readable position and quality together; readable quality zero means no fix. Missing quality means fix status unavailable.
3. Keep query failure, no fix and locally expired samples distinct. Never manufacture zero coordinates from an absent response.
4. Retain per-query receive times and source metadata; sequential GETs are not an atomic sensor snapshot.
5. Apply an explicit local retention policy, label it as collection age, and stop presenting old successful coordinates as fresh after failures.
6. Bound connection/read deadlines, total bytes, frame size and retries. A production reconnecting/subscribing client has not been validated by this work.

### Date and time

Date index `0xef` is days since 1970-01-01; UTC time index `0xee` is in 0.1 ms units since midnight. The Android producer divides location time by 86,400,000 ms, publishing time and then date separately. The consumer multiplies the day count by 86,400 and calls `gmtime_r`.

Date/time/date queries cannot guarantee atomic consistency at midnight because the producer also updates the two fields separately. Preserve the individual results and query times, reject inconsistent combinations and consider a later retry. Natural rollover and distant-date/2038 behavior remain unverified.

See the [measurement catalog](#measurement-catalog-and-units) for additional IDs and [evidence limits](#evidence-confidence-and-unresolved-work) for provenance. Private sources: EXP-013, EXP-022/023 navigation, EXP-029 date/no-fix and EXP-030 identity.

## Measurement catalog and units

Use the [native navigation protocol](#native-navigation-database-tcp-2051): request words are `[12, 0x100105, sequence, index]`, all little-endian u32. Returned item type and payload size must match the expected schema before decoding. Firmware units are independent of the display's selected units.

| Measurement | Index | Serialization type | Payload interpretation |
| --- | --- | --- | --- |
| Position | `0x44` | `0x3c` | `<ii` / 10,000,000 degrees |
| SOG | `0x04` | `0x01` | `<H` / 100 m/s |
| COG | `0x1a` | `0x2a` | `<H` / 10,000 radians; convert to degrees and wrap to [0,360) |
| Depth | `0x09` | `0x0e` | `<I` / 100 metres |
| Water temperature | `0x0f` | `0x12` | `<H` / 100 kelvins; subtract 273.15 for °C |
| WGS84 altitude | `0x80` | `0x63` | `<i` / 100 metres |
| Position accuracy / noise statistics | `0x156` | `0x13b` | First u16 / 100 metres; remaining bytes retained |
| Min/max depth | `0xb7` / `0xb8` | `0x0e` | `<I` / 100 metres |
| Min/max temperature | `0x170` / `0x171` | `0x12` | Same temperature decoding |
| Max/average SOG | `0x174` / `0x175` | `0x01` | `<H` / 100 m/s |
| Ground log / trips 1–4 | `0x104` / `0x105`–`0x108` | `0x0d` | `<I` metres |
| Sunrise / sunset | `0x184` / `0x185` | `0x05` | `<I` seconds in local day; date qualification above |


### GPS and satellites

The [GPS protocol table](#native-navigation-database-tcp-2051) documents position, quality, HDOP, in-use satellites and UTC date/time. Ordinary satellite details (`0x76`) and differential details (`0xfb`) were each 256 bytes: 32 slots of eight bytes.

| Slot offset | Representation | Meaning |
| --- | --- | --- |
| 0 | u8 | Satellite ID |
| 1 | u8 | Signal byte |
| 2 | u8 | Status byte |
| 3 | little-endian u16 | Azimuth |
| 5 | little-endian u16 | Elevation |
| 7 | u8 | Presence |

Preserve the raw ID, signal and status. Firmware also normalizes signal as raw/100; this does not establish dB-Hz. Presence in the table does not mean use in the fix. Ordinary (`0x77`) and differential (`0xfd`) in-use lists are separate 32-byte records. Sequential queries can describe different moments. Constellation mappings were checked in private analysis, but a complete public mapping is not supplied here; do not invent one from sample IDs.

### Display supply voltage

Over authenticated [SFTP](#sftp-access-and-storage), read:

```text
/sys/devices/platform/raymarine/secondary/vbatt
```

The value is an ASCII integer in millivolts. **Synthetic example:** `12000\n` represents 12.000 V. `PowerManagerAndroid::GetShipsSupplyVoltage` uses this path and a 0.001 multiplier. Decimal resolution is not a calibration guarantee.

A direct RayDB GET also returned voltage through:

```text
diag/mfd/PRODUCT_CODE SERIAL/product_info/supply_voltage
```

Replace the full identity component with your own device's value. The related `supply_current` returned NaN in the investigation, not zero. The investigated `secondary/ibatt` sysfs path was absent. No verified current, wattage or state-of-charge method follows from the voltage result.

### Interpretation limits

SOG is ground speed, not speed through water. COG near zero speed is not a reliable heading. WGS84 altitude is ellipsoidal height, not sea-level height. Depth reference, transducer identity and calibration were not independently established. Statistics and trip/log counters have unknown accumulation/reset intervals; do not present them as current samples.

The private investigation queried 117 mapped fields and received 36 records; 81 queries were unavailable. Tested unavailable families included battery monitoring, engine data, heading, wind, water speed and atmospheric measurements. This is a scoped observation, not proof that the firmware cannot expose them with suitable sources. External backbone data reception was not demonstrated.

Provenance: private EXP-022 navigation measurements, EXP-023 source/freshness/satellite follow-up and EXP-029 date validity. Actual values, geographic samples and stored journey counters are omitted from this public catalog.

## RayDB settings and diagnostics: TCP 23333

The remote adapter uses TCP 23333 and internally connects to loopback TCP 49111. The internal endpoint has different framing. RayDB returned settings, service status, alerts and diagnostics; a broad capture did not yield navigation measurements. Use [TCP 2051](#native-navigation-database-tcp-2051) for the verified navigation read path.

### Remote-adapter frame

All multi-byte numbers below are little-endian. Offsets are from the start of the complete frame, including its length word.

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 4 | Remaining frame length, excluding this u32 |
| 4 | 4 | Archive/class version: 1 in verified exchanges |
| 8 | 1 | Opcode: 7 HELLO, 0 GET, 1 SET, 4 UPDATE |
| 9 | 8 | Path byte length, u64 |
| 17 | variable | Path bytes, no terminator |
| after path | 1 | Regex flag |
| next | 1 | Remote durability |
| next | 1 | Remote scope |
| next | 4 | Variant type |
| next | variable | Variant payload, according to type |
| after payload | 4 | Counter, zero in documented requests |

The path length is **u64**, not a u32 length plus flags/padding. Older descriptions happened to work for short strings. Observed variant type **3 is unsigned u32**, while **7 is signed i32**; earlier decoder labels had their signedness reversed.

HELLO uses path `RayDBRemoteClient` (17 bytes), flags `00 01 01`, type 7, signed value zero and counter zero. An exact-key GET uses the requested path, flags `00 00 00`, type 7, zero value and zero counter.

The following offline example builds these requests; it performs no network access:

```python
import struct


def read_request(opcode, path):
    if opcode not in (0, 7):
        raise ValueError("This builder supports GET and HELLO only")
    path_bytes = path.encode("ascii")
    flags = bytes((0, 1, 1)) if opcode == 7 else bytes((0, 0, 0))
    body = (
        struct.pack("<IBQ", 1, opcode, len(path_bytes))
        + path_bytes + flags
        + struct.pack("<IiI", 7, 0, 0)
    )
    return struct.pack("<I", len(body)) + body

hello = read_request(7, "RayDBRemoteClient")
# Substitute your own identity locally; this string is a placeholder.
voltage_get = read_request(
    0, "diag/mfd/PRODUCT_CODE SERIAL/product_info/supply_voltage"
)
```

The successful collector sent HELLO, then known GET requests after a short bounded delay, and collected framed replies. A consumer must reassemble split/coalesced TCP data, bound frame/path lengths, validate the opcode/path and decode the returned variant type. This request builder is not a complete reply parser. In particular, do not decode every diagnostic reply as the integer type used in GET placeholders; returned values have their own types. Preserve nonfinite floating values as unavailable, not zero.

### Diagnostic paths

Known path shapes include:

```text
diag/mfd/PRODUCT_CODE SERIAL/product_info/product_name
diag/mfd/PRODUCT_CODE SERIAL/product_info/supply_voltage
diag/mfd/PRODUCT_CODE SERIAL/product_info/supply_current
sf/2/status/1/SOURCE_ID/-
```

Voltage was retrieved successfully. The tested current field returned NaN. The sounder-status key helped associate preferred depth/temperature sources with the local display. `SOURCE_ID` is an encoded serial; it is neither a portable constant nor anonymous telemetry.

A broad `#` capture was useful for discovery during research, but may expose identifiers and settings. Targeted known-key GETs are preferable for routine consumers. This guide does not provide a sanitized complete key catalog or complete variant decoder.

### Verified setting behavior

Theme key shape:

```text
Settings/Data/-/8/39/SOURCE_ID/-/-/-
```

A controlled test used SET opcode 1, flags `00 00 00`, type 3 (unsigned u32), value **0 = Light** or **1 = Dark**, and counter zero. Independent GET connections verified the change and restoration. SET itself produced no reply, so successful sending was not treated as proof of mutation.

The application write path uses corresponding zero durability/scope values, but restart persistence was not tested. Visual screen changes were not confirmed. This establishes one typed database mutation, not generalized support for every setting or control function.

Brightness has a different settings key (`.../8/46/SOURCE_ID/...`), and its saved luminance did not follow direct sysfs writes. Use [hardware readback](#remote-lcd-brightness-control) for those writes. Arbitrary settings mutation is outside the verified interface described here.

Provenance: private EXP-015 RayDB adapter, EXP-020 loopback protocol, EXP-023 voltage and EXP-029 theme investigation (including corrected u64 path length and variant signedness).

## SFTP access and storage

TCP 22 exposes an SSH service with a restricted `media_rw` account. Public-key authentication and SFTP worked; interactive shell and command execution were rejected. Authenticated forwarding was observed separately. Access to one file does not imply unrestricted filesystem access.

### Prerequisites and connection

The example assumes your public key is already enrolled and you have independently established the intended host identity. A mobile-access enrollment flow on TCP 8182 was demonstrated in the private research; a complete generic enrollment implementation is outside this guide. Do not assume a factory device accepts your key.

Keep the host, enrolled private-key path and verified known-hosts file local:

```sh
# Set ELEMENT_HOST and ELEMENT_KEY to your own local connection values first.
sftp -i "$ELEMENT_KEY" \
  -o IdentitiesOnly=yes \
  -o BatchMode=yes \
  -o UserKnownHostsFile=local/known_hosts \
  -o StrictHostKeyChecking=yes \
  -o ConnectTimeout=8 \
  -o PubkeyAcceptedAlgorithms=+ssh-rsa \
  -o HostKeyAlgorithms=+ssh-rsa \
  "media_rw@$ELEMENT_HOST"
```

The legacy RSA options were needed for the investigated OpenSSH 6.8 server; scope them to this connection. `local/known_hosts` must already contain the established host key. Do not bypass an unexpected host-key change. Private keys are never uploaded to the device or committed here.

Enrollment storage was associated with tmpfs and an initialization clear operation. Reboot persistence was not demonstrated; authentication failure after restart may require enrollment again. Do not confuse rejected authentication with changed host identity or failed network reachability.

### Useful paths

| Path | Observed use |
| --- | --- |
| `/mnt/internal_slot1` | SD-card mount; list, upload, download, rename and deletion verified |
| `/system/build.prop` | Read firmware/build properties |
| `/sys/devices/platform/raymarine/secondary/vbatt` | Read display input voltage in millivolts |
| `/sys/devices/platform/raymarine/backlight:lcd/` | Read brightness bounds/readback and write brightness |

Within SFTP, `get REMOTE_FILE LOCAL_FILE` downloads and `put LOCAL_FILE REMOTE_FILE` uploads. Upload can overwrite an existing file. Filesystem transfer does not trigger or prove GPX/route import; no remote automatic import operation was established.

Listings of `/data` and several protected directories were denied. A listed serial device is not an ordinary log file; the GPS serial stream was not consumed in this research. Existing native TCP queries provide navigation data without opening that stream.

Keep captures and host-specific settings outside public tracked content. See [brightness control](#remote-lcd-brightness-control) and [measurement units](#measurement-catalog-and-units) for sysfs behavior.

Provenance: private EXP-001/002 SFTP and SD-card tests, EXP-004 surface survey, EXP-005 enrollment persistence and EXP-007 import investigation.

## Remote LCD brightness control

Authenticated SFTP can write the LCD brightness sysfs attribute on the [investigated baseline](#architecture-and-compatibility). No shell or root access is required. The minimum setting was visibly confirmed in the original research. This is a state-changing operation; the code examples act on the display when invoked.

Use the prerequisites in [SFTP access](#sftp-access-and-storage): an already enrolled key and established host-key file are required. Replace the example host and key path locally.

### Hardware interface

All attributes below are in `/sys/devices/platform/raymarine/backlight:lcd/`.

| Attribute | Operation | Observed behavior |
| --- | --- | --- |
| `brightness` | Write ASCII integer plus newline | Mode 0666; ordinary SFTP `put` succeeds |
| `actual_brightness` | Read ASCII integer | Current driver readback; use this to verify writes |
| `min_brightness` | Read ASCII integer | Returned `1` |
| `max_brightness` | Read ASCII integer | Returned `10000` |

Always read bounds from the target. The mode-0666 `brightness` file itself returned a read failure; use `actual_brightness` instead. Do not interpret an empty or failed download as zero. The reported sysfs file size of 4096 is not the value's text length.

The firmware writes luminance ×1000, truncated to an integer. These raw values are **not UI percentages**. Raw zero was not tested: 0% in our control interface means the device's reported minimum, which was raw `1`. It does not mean display power-off.

The write payload is a short decimal text file, for example `1042\n`. Upload it directly to `brightness` using ordinary `put`, without append, resume, permission preservation, staging/rename or a remote shell. Sysfs attributes are driver interfaces, not ordinary files to replace atomically. A write can be applied even if a later transport operation fails, so confirm state through readback.

### Percentage conversion

The retained firmware's `GroupBrightnessManager::_getLuminanceForPc` and `_calcLuminanceForPc` use this curve:

```text
fraction = (10 ** ((percent / 100) ** 1.8) - 1) / 9
raw = int(min_raw + (max_raw - min_raw) * fraction)
```

Validate finite input in `[0, 100]`, truncate positive results with `int`, and constrain the result to the freshly read bounds. Explicitly return the endpoints for 0 and 100 to avoid floating-point endpoint drift.

| Requested percentage | Raw value for bounds 1–10000 |
| --- | --- |
| 0 | 1 |
| 50 | 1042 |
| 100 | 10000 |

This produces the **local display's firmware-curve equivalent**. Shared-brightness group bounds were not queried, and the on-screen slider was not observed to synchronize. Calling raw `5000` “50%” would use a different, linear scale. The formula is derived from private EXP-026 firmware analysis.

### Minimal Python implementation

This reference example uses Python's standard library and the local OpenSSH `sftp` executable. It consolidates the primitives exercised by the retained experiment scripts; it is not a separately live-tested packaged client. Defining these functions does not access or change the device. Calls to `set_percent` or `write_raw` do.

```python
import math
import subprocess
import tempfile
from pathlib import Path

HOST = "ELEMENT_HOST"  # Replace locally with the verified device host.
ROOT = "/sys/devices/platform/raymarine/backlight:lcd/"
SFTP = [
    "sftp", "-i", str(Path.home() / ".ssh/id_rsa"),
    "-o", "IdentitiesOnly=yes",
    "-o", "BatchMode=yes",
    "-o", "UserKnownHostsFile=local/known_hosts",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "ConnectTimeout=8",
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-b", "-", f"media_rw@{HOST}",
]


def quoted(path):
    # Quote for the SFTP batch parser, not a shell.
    text = str(path)
    if "\n" in text or "\r" in text:
        raise ValueError("Newline in path")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def batch(commands):
    result = subprocess.run(
        SFTP, input="\n".join(commands) + "\n",
        text=True, capture_output=True, timeout=18,
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)


def read_values(*names):
    allowed = {"actual_brightness", "min_brightness", "max_brightness"}
    if not names or not set(names) <= allowed:
        raise ValueError("Unsupported read attribute")
    with tempfile.TemporaryDirectory(prefix="element-brightness-") as folder:
        folder = Path(folder)
        batch([
            f"get {quoted(ROOT + name)} {quoted(folder / name)}"
            for name in names
        ])
        return tuple(int((folder / name).read_text().strip()) for name in names)


def limits():
    low, high = read_values("min_brightness", "max_brightness")
    if not 0 < low < high:
        raise ValueError(f"Unexpected hardware bounds: {low}, {high}")
    return low, high


def percent_to_raw(percent, low, high):
    percent = float(percent)
    if not math.isfinite(percent) or not 0 <= percent <= 100:
        raise ValueError("Percentage must be finite and between 0 and 100")
    if not 0 < low < high:
        raise ValueError("Invalid hardware bounds")
    if percent == 0:
        return low
    if percent == 100:
        return high
    fraction = (10 ** ((percent / 100) ** 1.8) - 1) / 9
    return max(low, min(high, int(low + (high - low) * fraction)))


def write_raw(value):
    low, high = limits()
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Raw value must be an integer in [{low}, {high}]")
    with tempfile.TemporaryDirectory(prefix="element-brightness-") as folder:
        payload = Path(folder) / "value.txt"
        payload.write_bytes(f"{value}\n".encode("ascii"))
        batch([f"put {quoted(payload)} {quoted(ROOT + 'brightness')}"])
    actual, = read_values("actual_brightness")
    if actual != value:
        raise RuntimeError(f"Brightness mismatch: requested {value}, got {actual}")
    return actual


def set_percent(percent):
    low, high = limits()
    return write_raw(percent_to_raw(percent, low, high))
```

Set and leave the display at 50% equivalent:

```python
actual = set_percent(50)
print(f"Hardware brightness now {actual}")
```

Optional control sequence, finishing at 50% even if an intermediate step raises:

```python
import time

try:
    set_percent(100)
    time.sleep(3)
    set_percent(0)
    time.sleep(3)
finally:
    set_percent(50)
```

The `finally` block attempts the final level; it cannot guarantee recovery if the host becomes unreachable or the process is killed. Do not suppress its errors. To restore a temporary experiment instead, capture `original, = read_values("actual_brightness")` before the first write and call `write_raw(original)` in `finally`. Restore the fresh raw value exactly, rather than converting it through a rounded percentage. For unattended tooling, save the baseline and intended final state durably before writing.

### Timing and verification

The original sequence used a new SFTP connection per write, slept three seconds after upload completion, and read back only the final level. The example above also verifies each intermediate write, adding another connection before each sleep. Neither method guarantees exactly three seconds between physical brightness transitions: authentication, upload, readback and scheduling add latency.

For tighter timing, investigate a persistent SFTP session with synchronous writes, monotonic deadlines and measured acknowledgement/readback latency. That optimization was not tested. Serialize control operations for this display; overlapping scripts or local user changes can invalidate a captured baseline or overwrite the selected value.

Treat a successful upload plus matching `actual_brightness` as the verification criterion. Log requested percentage, raw target, bounds, previous value, actual readback, UTC timestamps, elapsed times and errors. For research collections, retain raw outputs and SHA-256 manifests. On timeout or disconnection after a write, report state as unknown and attempt readback before deciding on another operation. Avoid unbounded retries or continuous reassertion that could fight local controls.

### Application state and persistence

Hardware control and application state are distinct. A temporary hardware change and restoration were verified by readback while the saved RayDB luminance remained unchanged. Its identity-bearing key has this shape:

```text
Settings/Data/-/8/46/SOURCE_ID/-/-/-
```

This device-specific key stores local brightness luminance. Its device component is not a portable constant. Use hardware readback to verify SFTP writes; a stale RayDB value does not mean the hardware write failed.

We have not established slider synchronization, persistence across restart, or what happens when the user next adjusts brightness locally. The application may overwrite a hardware-only change. RayDB has SET operations, but brightness mutations through RayDB have not been tested and are not part of this implementation. Do not write the key merely to make it match: the effects on live brightness and persistence are unresolved. Other display settings also need their own mapping and validation.


### Evidence and limits

Private EXP-023 brightness analysis establishes interface permissions, driver scaling and settings mapping; EXP-024 verified change/restoration; EXP-025 verified minimum brightness and visual dimming; EXP-026 traced the percentage curve and verified its midpoint; EXP-027 verified the control sequence. The consolidated Python example has not been separately live-tested as a packaged client.

Restart persistence, UI slider synchronization and conflicts with local controls remain unresolved. Actual device identifiers and saved baseline brightness values are deliberately omitted.

## Evidence, confidence and unresolved work

This repository is a public synthesis of the separate `raymarine-element` research project. Private source documents and artifacts were inspected locally to prepare these guides. No new hardware probes were performed for publication, and no raw captures or firmware binaries are distributed here.

### Provenance map

Experiment IDs below identify the private research records. They are labels, not downloadable public evidence. Repeated numbers in the source project refer to distinct topic-named investigations.

| Public guide / finding | Private research basis | Evidence class |
| --- | --- | --- |
| Platform and filesystem | EXP-001, EXP-004, EXP-008 system inventory, EXP-012 script/binary inspection | File reads and static analysis |
| SD-card operations and access lifetime | EXP-002, EXP-005, EXP-007 | File operations and startup/storage analysis |
| Native position/status framing | EXP-013 | Successful request/reply captures plus firmware handlers |
| Endpoint inventory | EXP-015–021 endpoint investigations | Bounded listener observations plus serializer/bind analysis |
| Measurement IDs and units | EXP-022 navigation | 117 queried fields; 36 available, 81 unavailable; firmware unit metadata |
| Source metadata, satellites and voltage | EXP-023 source/freshness | Targeted reads and serializer/source analysis |
| Brightness interface and curve | EXP-023 brightness, EXP-024–027 | Static analysis, writes/readback, minimum visibly confirmed |
| Theme mutation | EXP-028 settings, EXP-029 theme | Typed SET and independent GET restoration; UI effect unconfirmed |
| Date and no-fix behavior | EXP-029 GPS identity/date | Producer/consumer analysis; no natural loss-of-fix observation |
| Product/application identity | EXP-030 | Static producer, product table and serial-format analysis |
| GNSS driver and loader | EXP-031–033 | Fitted flag, installed driver and loader/extension analysis |

Captured real measurements and identifiers have been excluded. Protocol fields and algorithms are retained, with corrections from later investigations incorporated. Public readers can inspect the documented formats, but cannot independently replay the original private fixtures from this checkout. Synthetic examples illustrate encoding only and must not be described as live evidence.

### Confidence language

- **Observed/verified exchange:** the private research retains an actual request/reply or file-operation result for the stated baseline.
- **Static analysis:** firmware code supports the described behavior; it may not have been exercised live.
- **Inference/candidate:** evidence suggests a role, but runtime attribution or semantics remain incomplete.
- **Unknown/unverified:** the investigation does not establish the claim.

### Open questions

1. Naturally occurring fix loss/recovery, expiry and preferred-source switching: retain quality and position independently; establish behavior without assuming a static timeout branch is the entire lifecycle.
2. Date/time coherence across natural midnight and long-range timestamp handling.
3. Which external NMEA 2000/SeaTalkng sources become available and how instance-specific queries select them.
4. A reusable client with bounded reconnects, multi-chunk replies, unknown types, malformed frames and explicit freshness policy.
5. Complete satellite/variant catalogs and reviewed synthetic fixtures suitable for public offline tests.
6. Runtime service attribution for silent/dynamic ports and usable discovery behavior across interfaces.
7. Brightness/UI/persistence synchronization; visible theme effect and reboot durability.
8. Exact GNSS receiver model and independent firmware via an attributed status interface.
9. A reproducible generic public-key enrollment guide, including recovery and lifetime limits.
10. Compatibility across additional Element models and firmware builds.

No result here establishes a supported raw NMEA network gateway, every possible remote setting, remote route editing or screen mirroring. Negative results are scoped to the method and baseline, rather than statements that a capability can never exist.
