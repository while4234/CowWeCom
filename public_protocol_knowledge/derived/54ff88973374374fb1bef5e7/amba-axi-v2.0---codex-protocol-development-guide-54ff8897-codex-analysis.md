# AMBA AXI v2.0 Protocol Development Analysis Guide

> Analysis source: Codex manual analysis from `IHI0022C_amba_axi_v2_0_protocol_spec.pdf`.
> Project model backend used: false.
> model_backend_used=false.
> Protocol version scope: ARM IHI 0022C, AMBA AXI Protocol Specification, document Version 2.0 / Issue C.

## Scope And Source Map

This guide summarizes the AMBA AXI v2.0 memory-mapped protocol for local CowAgent public protocol knowledge. It is source-grounded in the extracted PDF text and is not a substitute for the original ARM specification.

Key source ranges:

- AXI design goals, revision scope, AXI4 and AXI4-Lite additions: pp. 15-16.
- Channel architecture, five independent channels, register slices, basic read/write examples, transaction ordering: pp. 17-23.
- Signal descriptions for global, write, read, response, and low-power interfaces: pp. 24-31.
- VALID/READY handshake rules and deadlock-prevention dependencies: pp. 32-38.
- Addressing, burst length, burst size, burst type, 4KB boundary rule, address and byte-lane calculations: pp. 39-46.
- Cache/protection attributes, atomic/exclusive/locked access, response signaling: pp. 47-59.
- ID-based ordering, outstanding transactions, read/write ordering, write data interleaving, interconnect ID handling: pp. 60-68.
- Data bus width, write strobes, narrow transfers, byte invariance, unaligned transfers: pp. 69-77.
- Clock/reset and optional low-power clock control: pp. 78-85.
- AXI4 deltas: longer bursts, QoS, regions, write response dependency, memory attributes, ordering, user signals, lock/write-interleaving removal, interoperability defaults: pp. 86-121.
- AXI4-Lite subset, conversion rules, protection, and detection: pp. 122-131.
- Issue C revision notes: p. 132.

## Development Picture

AXI is a burst-based, memory-mapped, master/slave interconnect protocol for high-bandwidth and low-latency SoC designs. Its central design choice is channel separation: address/control information is decoupled from read data, write data, and write responses. The split lets implementations pipeline channels independently, insert register slices for timing closure, support multiple outstanding transactions, and complete transactions out of order when IDs allow it. Source: pp. 15, 17-19.

For implementation and verification, model AXI as five cooperating channel contracts rather than as one monolithic bus:

- Read address channel `AR`: master sends read address and control.
- Read data channel `R`: slave returns read data, response, ID, and last indication.
- Write address channel `AW`: master sends write address and control.
- Write data channel `W`: master sends write data, byte strobes, optional write ID in AXI3, and last indication.
- Write response channel `B`: slave returns one response for a write burst.

The document examples show that read addresses can overlap with earlier read data and that write address, data, and response have separate progress points. Source: pp. 20-22. This means an implementation should avoid hidden global "transaction done" assumptions: read completion is the final read beat, while write completion is the write response, not the last accepted write beat. Source: pp. 21-22, p. 66.

## Revision 1.1 And Non-Applicable UCIe-Style Topics

The bundled audit tool checks for some UCIe-style topic markers, so this guide records their status explicitly. Revision 1.1 is not applicable to this AMBA AXI source; the relevant revision scope is AXI document Version 2.0 / Issue C. Appendix A says Issue C added chapter-layout material, extra VALID/READY constraints, a wrapping-burst equation, AXI4, AXI4-Lite, and the revisions appendix. Source: p. 132.

The following topics are not protocol features in this AXI source: protocol layer separation in the UCIe sense, D2D Adapter or die-to-die adapter, logical physical layer or logical PHY, electrical layer, sideband packet protocol, RDI, FDI, link training, retry, CRC, flit format, 68B, 256B, Streaming, and Raw Format. They are included here only as negative scope markers. AXI v2.0 instead defines memory-mapped channel signaling, cache/protection attributes, ordering, low-power clock control, AXI4, and AXI4-Lite. Source: pp. 15-16, pp. 24-31, pp. 86-131.

Configuration and register work in AXI is normally built on AXI4-Lite control register interfaces, not on DVSEC or protocol-owned configuration registers. Compliance for this source should be understood as protocol conformance: handshake legality, burst/address legality, response completion, ordering, reset, and AXI4/AXI4-Lite interoperability behavior. Source: pp. 32-38, pp. 87-121, pp. 122-131.

## Channel Ownership

### Global Signals

`ACLK` is the common clock and all signals are sampled on its rising edge. `ARESETn` is active low. The clock/reset chapter adds that output changes occur after the rising edge of `ACLK`, there must be no combinatorial input-to-output paths on master or slave interfaces, reset can assert asynchronously, and reset deassertion must be synchronous to `ACLK`. During reset, master `ARVALID`, `AWVALID`, and `WVALID` must be low; slave `RVALID` and `BVALID` must be low. Source: pp. 25, 79.

Verification focus:

- Assert all VALID outputs required low during reset.
- Assert reset deassertion synchronization at the interface boundary.
- Check no combinational dependency from READY input to VALID output and no combinational dependency from VALID input to READY output when the implementation claims fully registered interfaces. Source: pp. 33, 79.

### Write Address Channel

The write address channel carries `AWID`, `AWADDR`, `AWLEN`, `AWSIZE`, `AWBURST`, `AWLOCK`, `AWCACHE`, `AWPROT`, `AWVALID`, and `AWREADY` in the AXI3-style signal table. `AWADDR` is only the address of the first transfer; burst controls determine later addresses. `AWVALID` means address/control are stable until `AWREADY` accepts them. Source: p. 26.

For AXI4, `AWLEN` widens to 8 bits and optional signals such as `AWREGION`, `AWQOS`, and `AWUSER` can appear depending on interface category and system use. Source: pp. 87-90, pp. 113, pp. 118-120.

### Write Data Channel

The write data channel carries `WDATA`, `WSTRB`, `WLAST`, `WVALID`, `WREADY`, and in AXI3, `WID`. `WSTRB[n]` maps to the corresponding byte lane of `WDATA`, and the master must assert `WLAST` on the final write transfer of a burst. Source: pp. 27, 34, p. 71.

AXI4 removes `WID`; all write data for a transaction must be consecutive and ordered relative to the write addresses. For AXI3-to-AXI4 compatibility, a legacy AXI3 master that supports write interleaving must be configured to interleaving depth 1. Source: p. 115.

### Write Response Channel

The write response channel carries `BID`, `BRESP`, `BVALID`, and `BREADY`. The slave produces one write response for an entire burst. Source: pp. 28, 58. AXI4 adds a stronger dependency: a slave must not assert `BVALID` until after both write address acceptance and final write data beat acceptance. The response also must not wait for `BREADY`; the master may wait for `BVALID` before asserting `BREADY`. Source: pp. 91-92.

### Read Address And Read Data Channels

The read address channel mirrors the write address role with `ARID`, `ARADDR`, `ARLEN`, `ARSIZE`, `ARBURST`, `ARLOCK`, `ARCACHE`, `ARPROT`, `ARVALID`, and `ARREADY`. Source: p. 29. The read data channel carries `RID`, `RDATA`, `RRESP`, `RLAST`, `RVALID`, and `RREADY`; the slave must match `RID` to the accepted `ARID` and assert `RLAST` for the final beat. Source: p. 30, p. 35.

Unlike writes, reads can return a response per beat. Even after an error response, the required number of read transfers must complete; the burst is not cancelled by a single error. Source: p. 58.

## VALID/READY Handshake Rules

All five data-transfer channels use the same two-way `VALID` and `READY` handshake. Transfer happens only in cycles where both are high. The source asserts `VALID` when data or control is available; the destination asserts `READY` when it can accept. Source: p. 33.

The most important deadlock rule is asymmetric: the source must not wait for `READY` before asserting `VALID`, but the destination may wait for `VALID` before asserting `READY`. Once asserted, `VALID` must remain asserted until handshake. A destination that asserts `READY` early may deassert it before `VALID` appears. Source: p. 33, p. 37.

For reads, the slave may wait for `ARVALID` before asserting `ARREADY`, but it must wait for the address handshake before asserting `RVALID`. Source: p. 37. For writes, the master must not wait for `AWREADY` or `WREADY` before asserting `AWVALID` or `WVALID`. This specific write rule prevents deadlock when a slave waits for both address/data intent before accepting. Source: pp. 37-38.

Verification focus:

- Assert each channel holds payload stable while `VALID && !READY`.
- Cover the three legal timing cases: VALID before READY, READY before VALID, and simultaneous assertion.
- Assert that masters do not gate `AWVALID` on `AWREADY`, `WVALID` on `WREADY`, or `ARVALID` on `ARREADY`.
- Assert read data starts only after address acceptance, and write response starts only after the required address/data acceptances for the selected AXI generation. Source: pp. 33-38, p. 91.

## Burst Addressing

AXI uses burst addressing. The master sends the start address and controls; the slave calculates later addresses. Bursts must not cross 4KB boundaries, both to avoid crossing slave boundaries and to keep slave address incrementers bounded. Source: p. 40.

In the base AXI3-style description, `AxLEN[3:0]` encodes 1 to 16 transfers. Wrapping bursts must have length 2, 4, 8, or 16. A component cannot terminate a burst early; writes can deassert all strobes for unwanted beats and reads can discard unwanted data, but the interface still completes the specified beats. Source: p. 41. `AxSIZE` gives the maximum bytes per beat and must not exceed the data bus width. Source: p. 42.

Burst types are:

- `FIXED`: constant address, typically FIFO-style accesses.
- `INCR`: incrementing address for normal sequential memory.
- `WRAP`: incrementing address that wraps at a cache-line style boundary.

Source: pp. 43-44. Address and byte-lane equations use the start address, transfer size, burst length, aligned address, and wrap boundary to derive each beat address and the data bits used. Source: pp. 45-46.

AXI4 extends `AWLEN` and `ARLEN` to 8 bits, supporting up to 256 beats. Bursts longer than 16 beats are only legal for `INCR`; `WRAP` and `FIXED` remain limited to 16, and exclusive accesses cannot use a burst length greater than 16. Source: p. 87.

## Cache, Protection, And Memory Attributes

The base `AxCACHE[3:0]` fields describe bufferable, cacheable, read-allocate, and write-allocate attributes. The bufferable bit allows an interconnect or component to delay arrival at the final destination; for writes, a bufferable transaction may allow an intermediate write response. The cacheable bit permits transformed behavior such as merging writes or prefetching reads. Allocate bits are hints for cache behavior and must not be high when cacheable is low in the base encoding. Source: pp. 48-49.

`AxPROT[2:0]` carries normal/privileged, secure/non-secure, and instruction/data information. The instruction/data bit is a hint; the spec recommends defaulting to data unless an instruction access is specifically known. Source: p. 50.

AXI4 renames `AxCACHE[1]` to `Modifiable`. If it is low, the transaction is Non-modifiable and must not be split, merged, or have key parameters changed, with the special allowance that long bursts above 16 may be broken into smaller bursts while preserving other characteristics. If it is high, the transaction may be split, merged, over-fetched, widened with strobes, or otherwise transformed within the defined limits. Lock and protection type must not be changed, and modifications must not cross a 4KB address space or break single-copy atomicity rules. Source: pp. 93-94.

AXI4 also refines allocate/cache lookup meaning. If either allocate-related bit in `AxCACHE[3:2]` indicates possible allocation, the transaction must be looked up in a cache; if both are low, lookup is not required. Source: pp. 96-98. AXI4 names memory types such as Device Non-bufferable, Device Bufferable, Normal Non-cacheable, Write Through, and Write Back, with legal encodings listed in the table. Source: p. 99.

Verification focus:

- Check reserved cache encodings are not generated by masters unless intentionally unsupported and error-handled.
- Check Non-modifiable transactions preserve fixed parameters through interconnect transforms.
- Check Device transactions with same ID to the same slave remain ordered.
- Check `AxPROT[1]` secure/non-secure assignment carefully because incorrect use can change system security behavior. Source: pp. 93-100, p. 121.

## Atomic, Exclusive, And Locked Accesses

The base AXI protocol uses `AxLOCK[1:0]` to distinguish normal, exclusive, and locked accesses. Source: p. 52. Exclusive access is designed for semaphore-like operations without locking the entire bus. A master performs an exclusive read, then later an exclusive write to the same location. The slave returns `EXOKAY` when the exclusive operation succeeds and `OKAY` when it fails or when exclusive access is unsupported. Source: p. 53.

A slave supporting exclusive access needs monitor hardware. The monitor records address and `ARID` on an exclusive read, then checks the matching `AWID` exclusive write. If the monitored location is still valid, the write updates memory and returns `EXOKAY`; otherwise it must not update the address and returns `OKAY`. Source: p. 54.

Exclusive restrictions include same size/length for read and write, identical address, matching `ARID`/`AWID`, identical control signals, a power-of-two byte count, maximum 128 bytes, and non-cacheable visibility to the monitoring slave. Failure to observe these restrictions is unpredictable. Source: pp. 54-55.

Locked access is a legacy mechanism where the interconnect excludes other masters from the slave region until an unlocking transaction completes. It requires no other outstanding transactions at sequence start, same ID through the sequence, and careful completion before later transactions. Source: p. 56. AXI4 removes locked transaction support and uses a 1-bit lock encoding: normal or exclusive. AXI3 locked transactions converted into AXI4 become normal transactions, and components that rely on the locked semantics cannot be used in AXI4. Source: p. 114.

## Response Signaling

`BRESP` gives one response for a write burst; `RRESP` accompanies each read beat. The defined responses are `OKAY`, `EXOKAY`, `SLVERR`, and `DECERR`. `OKAY` means normal success and can also mean exclusive failure. `EXOKAY` means exclusive success. `SLVERR` means the addressed slave accepted the access but reports an error. `DECERR` is typically generated by interconnect/default-slave decode failure. Source: pp. 58-59.

The protocol requires completion of all transfers even when an error occurs. For a read burst, an error on one beat does not cancel the rest of the burst; a component returning `DECERR` must still meet the completion requirement. Source: pp. 58-59.

Verification focus:

- Assert write response count is exactly one per accepted write burst.
- Assert read response count equals `ARLEN + 1` for every accepted read burst.
- Check `RLAST` aligns with the final beat even on errors.
- Check default slave `DECERR` behavior cannot deadlock the system. Source: pp. 58-59.

## Ordering And IDs

AXI uses ID fields so a port can behave as multiple ordered streams. All transactions with a given ID must preserve their required ordering, while transactions with different IDs have fewer restrictions and may complete out of order. Source: pp. 22, 61-62.

For reads, data with the same `ARID` must return to the master in address issue order. Data with different `ARID` values can return in any order and can be interleaved. The slave must return `RID` matching the addressed `ARID`; an interconnect must preserve same-ID ordering even if same-ID reads target different slaves. Source: p. 63.

For writes in the base protocol, if a slave does not support write data interleaving, write data must arrive in the same order as write addresses, even across different `AWID` values. Write data interleaving is allowed only for different `AWID` values, and never for the same `AWID`; a slave with interleaving depth greater than one must continuously accept interleaved write data and must not stall to reorder it. Source: pp. 64-65.

There are no inherent ordering restrictions between reads and writes, even with matching `AWID` and `ARID`. If a master needs a relationship between a read and write, the earlier transaction must be complete before issuing the later one. For writes, completion means receiving the write response. Source: p. 66.

Interconnects commonly append master-port bits to `ARID`, `AWID`, and AXI3 `WID` so slave-side IDs are unique across masters; they remove appended bits when routing responses back. Source: p. 67. The spec recommends up to four transaction ID bits in masters, up to four additional interconnect bits, and eight ID bits in slaves for out-of-order use. Source: p. 68.

AXI4 ordering tightens Device and Non-modifiable cases. Non-modifiable transactions with the same ID to the same slave must remain ordered, regardless of address. For Device memory, same-ID transactions must arrive at the device in issue order; for Normal memory, same-ID transactions to same or overlapping addresses must arrive in issue order. Cross-direction ordering still requires waiting for the earlier response. Source: pp. 95, 108-110.

## Data Bus, Strobes, Narrow Transfers, And Byte Invariance

AXI has independent read and write data buses, so read and write data transfers can occur in the same cycle. A transfer generated by a master must be no wider than the data bus. Source: p. 70.

Write strobes make sparse byte updates possible. Each strobe bit maps to one byte lane; a master must assert strobes only for byte lanes valid under the transaction address/control information. Source: p. 71. Narrow transfers use address and control fields to select byte lanes; fixed bursts keep lanes constant while incrementing and wrapping bursts move across lanes. Source: p. 72.

The byte-invariant endianness rule means a byte at a given address travels on the same data bus byte lane independent of endian interpretation. This supports mixed-endian structures where part of a packet might be little-endian metadata and another part a big-endian byte stream. Source: p. 73.

Unaligned transfers are allowed by using low-order address bits to signal an unaligned start address, and write strobes must be consistent with those address bits. The spec does not require the slave to take special action based on alignment information; a master may instead present an aligned address and use strobes to indicate active byte lanes. Source: p. 75.

Verification focus:

- Assert `WSTRB` is legal for address, size, burst type, and beat index.
- Cover narrow fixed, incrementing, and wrapping bursts.
- Check byte-lane mapping for mixed widths and unaligned starts.
- Check slaves that ignore strobes are not used for memory-like behavior requiring sparse writes. Source: pp. 71-75, p. 126.

## Low-Power Interface

The optional low-power interface targets peripherals that either need explicit power-down sequencing before clocks stop or can independently indicate clock-disable readiness. Source: p. 81. `CACTIVE` is driven by the peripheral to request/require clock activity. `CSYSREQ` is driven by the system clock controller to request entry or exit from low power. `CSYSACK` acknowledges both entry and exit. Source: pp. 31, 82.

Normal operation has `CSYSREQ` and `CSYSACK` high. To request low power, the controller drives `CSYSREQ` low; the peripheral eventually drives `CSYSACK` low. The `CACTIVE` level when the request is acknowledged indicates acceptance or denial. Either controller or peripheral can initiate low-power exit; if the peripheral drives `CACTIVE` high, the controller must restore the clock immediately and continue the handshake. Source: pp. 82-84.

When multiple peripherals share a low-power clock domain, the domain `CACTIVE` is the OR of peripheral `CACTIVE` signals, a single `CSYSREQ` may be broadcast, and domain `CSYSACK` edges occur after all participating peripheral acknowledgements. Source: p. 85.

## AXI4 Implementation Notes

AXI4 is the update chapter for designers already familiar with AXI3. Its key deltas are longer bursts, QoS, region signals, stronger write response dependencies, renamed/refined cache attributes, clarified ordering, optional user signals, removal of locked transactions, removal of write interleaving, and interoperability/default signal rules. Source: pp. 15, 86.

`AWQOS` and `ARQOS` are 4-bit transaction-level identifiers, preferably priority values. The protocol does not define a complete QoS algorithm; system-level policy owns interpretation, and normal AXI ordering rules override QoS ordering. Source: pp. 88-89.

`AWREGION` and `ARREGION` are optional 4-bit region identifiers allowing up to sixteen decode regions behind one physical slave interface. They must be consistent with the address space and remain constant within a 4KB address space. A slave remains responsible for protocol and ordering correctness for all region values, including unsupported regions. Source: p. 90.

User signals `AWUSER`, `ARUSER`, `WUSER`, `RUSER`, and `BUSER` are implementation-defined. The spec generally discourages generic use because incompatible user semantics can harm interoperability. Interconnect support is recommended when user signals are used so they can be passed between components. Source: p. 113.

Default signal values are a major AXI4 integration feature. Components need not generate every optional output if their behavior matches the default, and they may omit optional inputs they do not need for correct operation. However, memory slaves must handle all transaction types correctly, while peripheral slaves may define narrower legal access methods but still must complete out-of-range or unsupported accesses in a protocol-correct way to avoid deadlock. Source: pp. 116-121.

## AXI4-Lite Development Notes

AXI4-Lite is the control-register subset of AXI4. It keeps the channel handshake structure but removes complexity: burst length is always one, data width is 32 or 64 bits, accesses are the same size as the data bus, cache attributes are equivalent to `b0000`, and exclusive accesses are unsupported. Source: pp. 122-125.

Unsupported or reduced signals include `AxLEN`, `AxSIZE`, `AxBURST`, `AxLOCK`, `AxCACHE`, `WLAST`, `RLAST`, and `EXOKAY` responses. IDs are not supported in native AXI4-Lite, so all accesses are ordered under a single fixed ID, though optional ID reflection can let a Lite slave sit behind a full AXI interface when the system guarantees only Lite-subset accesses. Source: pp. 124-127.

AXI4-Lite write strobes are supported. Masters and interconnects must provide correct strobes; slaves may fully use them, ignore them and treat writes as full width, or reject unsupported combinations with an error. Memory-like slaves must fully support write strobes. Source: p. 126.

Full AXI to AXI4-Lite conversion can split bursts into length-one transactions, split wide transfers into Lite-width transactions, combine sticky responses, pass narrower transactions directly, pass strobes directly, discard locks/cache attributes safely, and preserve protection bits. Source: pp. 128-129. Protection-only designs may suppress unsupported accesses and return protocol-compliant errors, while detection can notify software without blocking unexpected but converted accesses. Source: pp. 130-131.

## Verification And Testbench Checklist

High-value testpoints:

- Reset: VALID outputs low during reset and first assertion after synchronous reset deassertion. Source: p. 79.
- Handshake: VALID cannot depend on READY; payload stable under backpressure; READY default high and low cases covered. Source: pp. 33-38.
- Burst legality: no 4KB crossing, legal `AxLEN`, legal `AxSIZE`, legal `AxBURST`, correct fixed/incrementing/wrapping addresses, no early termination. Source: pp. 40-46, p. 87.
- Write channel relationship: address and data can arrive in either order, but response must follow address acceptance and final data beat acceptance in AXI4. Source: p. 36, pp. 91-92.
- Read channel relationship: read data only after read address acceptance, `RID` matches `ARID`, and `RLAST` appears on the final beat. Source: pp. 35, 37, p. 63.
- Response behavior: one `BRESP` per write burst, one `RRESP` per read beat, all beats complete on errors, default slave returns `DECERR` without deadlock. Source: pp. 58-59.
- Exclusive access: monitor per ID/address behavior, success and fail paths, unsupported exclusive returning `OKAY`, identical read/write attributes, max 128 bytes. Source: pp. 53-55.
- Ordering: same-ID read order, same-ID write response order, no accidental read/write ordering without response barrier, Device and Non-modifiable AXI4 ordering. Source: pp. 61-66, pp. 95, 108-110.
- Data strobes: legal `WSTRB` under unaligned and narrow transfers; memory-like Lite slaves fully support strobes. Source: pp. 71-75, p. 126.
- AXI4 compatibility: no `WID`, no locked transactions, QoS cannot override ordering, region signals stay address-consistent. Source: pp. 88-90, pp. 114-115.
- AXI4-Lite: length-one accesses, no exclusive/EXOKAY, ordered single fixed ID, proper full-AXI conversion or protection responses. Source: pp. 124-131.

## Terminology Map

- `VALID/READY`: per-channel handshake signals; transfer occurs when both are high. Source: p. 33.
- `LAST`: `WLAST` or `RLAST`, final beat marker for burst data channels. Source: pp. 27, 30, 34-35.
- `ID`: `AWID`, `WID`, `BID`, `ARID`, `RID`; ordering and response routing tags. Source: pp. 61-68.
- `Burst`: sequence described by start address, length, size, and type. Source: pp. 40-46.
- `FIXED`, `INCR`, `WRAP`: three burst address behaviors. Source: pp. 43-44.
- `WSTRB`: byte-lane write-enable strobes. Source: p. 71.
- `OKAY`, `EXOKAY`, `SLVERR`, `DECERR`: response encodings. Source: pp. 58-59.
- `Non-modifiable`: AXI4 transaction whose core parameters must not be changed except specific allowed long-burst splitting. Source: pp. 93-95.
- `Device` and `Normal` memory: AXI4 memory type categories with different visibility and ordering requirements. Source: pp. 99-110.
- `AXI4-Lite`: reduced control-register interface subset of AXI4. Source: pp. 122-131.
