# UCIe 1.1 Protocol Development Analysis Guide

> Analysis source: Codex manual analysis from `eetop.cn_UCIe_1p1_with_legal_disclaimer_July_10th_2023.pdf`.
> Project model backend used: false.
> Protocol version: UCIe Specification Revision 1.1, Version 1.0, dated July 10, 2023.

## Scope And Source Map

This document is a protocol-development guide for building UCIe 1.1 knowledge and verification work products. It is derived from the public UCIe 1.1 specification text extracted locally. It is not a replacement for the original specification.

Key source ranges:

- Terminology, references, revision history: pp. 18-24.
- Architecture introduction, component split, package characteristics: pp. 25-35.
- Protocol Layer behavior and protocol-to-flit-mode mapping: pp. 36-42.
- Die-to-Die Adapter behavior, parameter exchange, flit formats, retry, CRC, runtime link testing: pp. 43-77.
- Logical Physical Layer, lane mapping, repair, link training, power states: pp. 78-143.
- Electrical layer and package-specific electrical constraints: pp. 144-194.
- Sideband packet protocol, register access, flow control, data integrity: pp. 195-219.
- Configuration, discovery, DVSEC/register blocks, health/compliance controls: pp. 220-286.
- RDI and FDI interface definitions and state/clocking rules: pp. 287-329.
- Compliance capabilities and Golden Die expectations: pp. 330-332.
- CXL/PCIe register applicability and AIB interoperability appendices: pp. 333-338.

## UCIe 1.1 Development Picture

UCIe is an on-package, multi-protocol die-to-die interconnect. It carries PCIe, CXL, Streaming protocol, and Raw Format traffic over a common UCIe link. The specification separates protocol semantics from flit packing, retry, CRC, link training, physical operation, and sideband control. That separation should also drive the implementation split. See pp. 25-30 and pp. 36-37.

For protocol development, treat UCIe as four cooperating planes:

- Data plane: Protocol Layer packets or flits enter FDI, are adapted into negotiated UCIe flit formats, cross mainband lanes, and are delivered back through FDI. Relevant source: pp. 36-42, pp. 55-68, pp. 301-306.
- Control plane: sideband messages negotiate capabilities, read/write local or remote registers, coordinate link state, and support debug/compliance. Relevant source: pp. 46-51 and pp. 195-219.
- Link-management plane: D2D Adapter and Logical PHY state machines bring up, retrain, repair, reverse, degrade, and power-manage the link. Relevant source: pp. 70-77 and pp. 102-143.
- Software/configuration plane: DVSEC and UCIe register blocks expose discovery, capability, status, error, health, test, and compliance controls. Relevant source: pp. 220-286.

## Revision 1.1 Items To Track

The revision history identifies the main 1.1 deltas as Streaming Flit Format capability, enhanced multi-protocol multiplexing, x32 Advanced Package modules, UCIe Link Health Monitoring, hardware capabilities for compliance, di/dt mitigation during clock gating, and errata/bug fixes over 1.0. Source: p. 24.

Development implications:

- Streaming support is no longer only a raw transport concern; Streaming Flit Format capability allows the Adapter to provide retry/CRC services to Streaming traffic when negotiated. Source: pp. 36-42 and pp. 46-48.
- Enhanced multi-protocol support permits dynamic multiplexing of two stacks that may use different protocols, provided they share at least one common flit format and negotiate the required capability bits. Source: pp. 47-48 and pp. 43-45.
- x32 Advanced Package support affects lane counts, repair resources, mapping, package interoperability, and register-visible link configuration. Source: pp. 28-30, pp. 78-97, pp. 162-178, pp. 255-268.
- Link health and compliance capabilities must be visible through register/test structures, not only as lab-only debug hooks. Source: pp. 269-283 and pp. 330-332.

## Layer Ownership

### Protocol Layer

The Protocol Layer may be PCIe, CXL, Streaming, or application-specific. UCIe gives examples for PCIe and CXL transport and defines generic modes for user-defined Streaming protocols. Source: p. 29.

Protocol Layer implementation responsibilities:

- Select supported protocol families and advertise legal combinations through the Adapter capability flow.
- Drive FDI data according to the negotiated mode and flit format.
- Follow UCIe-specific transport rules where protocol features and flit packing are intentionally separated.
- For CXL, understand that CXL.io, CXL.cache, and CXL.mem protocol details can be negotiated independently when CXL is selected. Source: p. 36.
- Treat PCIe non-Flit mode over UCIe as transported through a CXL.io 68B flit mechanism, as described in the Protocol Layer chapter. Source: p. 36.

Interoperability rules that should become feature gates:

- Advertising 68B Flit Mode implies support for PCIe non-Flit mode.
- CXL 256B support implies the related CXL.io requirements for PCIe Flit Mode and 68B Flit Mode.
- A CXL-advertising Protocol Layer may support only CXL 68B without CXL 256B or PCIe Flit Mode. Source: p. 36.

### Die-to-Die Adapter

The D2D Adapter is the key protocol-development boundary. It coordinates the Protocol Layer and Physical Layer, keeps the main data path low latency, and implements features such as arbitration/muxing, CRC/retry or parity, parameter exchange, link state management, and power-management coordination. Source: pp. 29 and 43.

Adapter development responsibilities:

- Negotiate protocol, flit format, retry, raw mode, retimer, stack enablement, and enhanced multi-protocol capability through sideband messages.
- Inform the Protocol Layer of the negotiated protocol and flit format over FDI.
- Insert or interpret Adapter-owned flit fields, CRC, retry behavior, NOP flits, test flits, and stack identifiers.
- Maintain Adapter Link State Machines, including multi-stack behavior where applicable.
- Bridge sideband register access between local FDI/RDI paths and remote link partners.

The Adapter capability exchange begins after local capability determination, advertises capabilities using sideband messages, and finalizes common protocol/flit parameters with the remote partner. Source: pp. 46-51.

### Logical Physical Layer

The Logical PHY sequences byte/lane mapping, sideband transfer, mainband training, lane repair, lane reversal, retrain, and power state behavior. Source: pp. 30 and 78-143.

Logical PHY development responsibilities:

- Implement deterministic byte-to-lane mapping for x64, x32, x16, and degraded configurations.
- Support lane reversal and repair rules appropriate to Standard or Advanced Package.
- Support data-to-clock training, LFSR pattern generation, per-lane and aggregate comparison, and training result readback through sideband.
- Implement the Link Training state machine and state-specific sideband messages.
- Expose status and controls needed by Adapter and software-visible registers.

Training should be modeled as a sideband-coordinated sequence in which one die configures pattern generation/comparison, starts or sweeps test patterns, requests result logs, and ends the sequence. Source: pp. 97-105.

### Electrical Layer

The Electrical Layer defines the signaling and package-specific constraints needed by the PHY. Protocol development should not bury those constraints inside Adapter logic; keep them in PHY/package configuration and feed summarized status upward. Source: pp. 144-194.

Important implementation axes:

- Standard Package and Advanced Package have different bump pitch, reach, module width, repair, and BER characteristics. Source: pp. 27-28 and pp. 162-190.
- Advanced Package supports x64 and x32 modules; Standard Package uses x16 modules without the same repair pins. Source: pp. 28-30.
- BER target changes by speed and package type, which feeds retry/CRC decisions. Source: pp. 27-28 and pp. 190-194.

### Sideband

Sideband is used for parameter exchange, debug/compliance register access, link training coordination, and link management. It is separate from mainband and remains available through an auxiliary always-on domain. Source: pp. 20-21 and pp. 29-30.

Sideband development responsibilities:

- Implement packet types and formats for register access, messages without data, and messages with data payloads.
- Enforce credit rules on FDI/RDI sideband traffic and remote link sideband paths.
- Guarantee unconditional sinking for register access completions.
- Map sideband parity/control errors to fatal uncorrectable internal errors where required.
- Limit outstanding remote mailbox transactions and guarantee forward progress. Source: pp. 195-219.

Sideband over the UCIe sideband link has no retry mechanism in the spec because the sideband BER target is 1e-27 or better; receivers must detect parity errors and escalate them. Source: p. 216.

## Interfaces To Implement

### RDI

RDI connects the Adapter and Physical Layer. It carries raw die-to-die data/control and exposes reset, clocking, data transfer, state machine, bring-up, and power-management behavior. Source: pp. 287-300.

Development guidance:

- Keep RDI state handling separate from FDI state handling, even though Adapter logic must bridge them.
- Treat RDI bring-up and PM flows as protocol-observable events through Adapter status and FDI state.
- Validate RDI timing/state rules with assertions that model reset, active, PM, retrain, disabled, and link error cases.

### FDI

FDI connects the Protocol Layer and Adapter. It is flit-aware and includes data-valid/ready, stream identification, DLLP movement, cancellation, state request/status, protocol/flit-format indication, error signals, active receive handshake, stall handshake, retrain/PM indications, link speed/configuration, and clock-gating handshakes. Source: pp. 301-306.

Implementation details to model explicitly:

- Data transfer uses `lp_valid`, `lp_irdy`, and `pl_trdy` for Protocol-to-Adapter acceptance.
- `lp_stream` and `pl_stream` identify stack/protocol stream encodings.
- `pl_protocol` and `pl_protocol_flitfmt` expose negotiated protocol/format and must be sampled only when valid under the specified state conditions.
- `pl_flit_cancel` allows Adapter-side CRC/retry cancellation for applicable 256B flit formats; canceled data must later replay correctly.
- Clock gating uses `pl_clk_req/lp_clk_ack` and `lp_wake_req/pl_wake_ack` four-way handshakes. Source: pp. 302-306.

## Protocol And Flit-Format Matrix

Build a negotiation model around these protocol families:

- PCIe Flit Mode.
- PCIe non-Flit Mode transported through CXL.io 68B flit format.
- CXL 68B Flit Mode.
- CXL 256B Flit Mode.
- Streaming Protocol in Raw Format or Streaming Flit Formats.
- Raw Format for implementation-specific or user-defined protocols when both ends support it. Source: pp. 36-42 and pp. 46-48.

Recommended internal representation:

```text
protocol_family: pcie | cxl | streaming | raw
protocol_mode: pcie_flit | pcie_non_flit | cxl_68b | cxl_256b | streaming
flit_format: raw | 68b | std_256b_end | std_256b_start | latopt_256b_no_optional | latopt_256b_optional
stack_id: 0 | 1
retry: enabled | disabled
crc_owner: adapter | protocol_layer
```

Negotiation must reject combinations that cannot be supported by both link partners, both protocol stacks, and the current physical/link state. Source: pp. 46-51 and pp. 68-70.

## Link Bring-Up Milestones

Use the following milestones for implementation planning:

1. Domain reset and local capability initialization.
2. Sideband availability and remote partner discovery.
3. Physical training, lane repair, lane reversal, and width/speed establishment.
4. Adapter capability advertisement and finalization.
5. Protocol/flit-format selection and FDI indication.
6. FDI/RDI active-entry handshakes.
7. Runtime operation with retry/CRC, error logging, PM, retrain, and optional runtime link testing.

These milestones align with component responsibilities described in the introduction, Adapter link initialization, Logical PHY link training, and FDI/RDI interface chapters. Sources: pp. 28-30, pp. 46-55, pp. 102-143, pp. 295-315.

## Error, Retry, And Recovery Work Items

Adapter retry/CRC is central to UCIe Flit Mode. UCIe Raw Format keeps all flit bytes under Protocol Layer ownership, while UCIe Flit Mode inserts/checks CRC bytes in the Adapter and may perform Adapter retry. Source: pp. 21-22 and pp. 55-68.

Implementation work items:

- CRC generation/checking by flit format.
- Tx retry buffer sizing and overwrite/cancel handling.
- Ack/Nak and timeout behavior.
- Correctable and uncorrectable error status/mask/severity registers.
- Header/syndrome logging for timeout, overflow, and state machine errors.
- Runtime link testing parity insertion/checking. Source: pp. 73-77 and pp. 245-252.

The register model should capture timeout reasons, receiver overflow causes, Adapter LSM response type, negotiated flit format, first fatal error indication, and runtime link test controls. Source: pp. 248-250.

## Register And Software Model

Software discovers UCIe links and accesses UCIe registers through the structures in Chapter 7. The register model includes UCIe Link DVSEC, switch register block, D2D/PHY register block, test/compliance register block, UHM, streaming-mode implications, MSI/MSI-X, and UEDT. Source: pp. 220-286.

Register development checklist:

- UCIe Link DVSEC capability/control/status.
- Link event and error notification controls.
- Sideband mailbox index/data/control/status.
- D2D/PHY error, log, capability, finalized capability, PHY capability/control/status, training setup, lane map, repair, runtime link test, and UHM registers.
- Test/compliance controls for Adapter and PHY.
- Streaming mode register interpretation.

Do not treat registers as passive documentation. Many fields feed protocol behavior: negotiated capability logs, training setup, runtime link testing, link health, error escalation, and compliance injection paths.

## Verification Focus

### Protocol Layer Verification

- Protocol/flit-mode legal-combination matrix.
- PCIe/CXL/Streaming/Raw mode negotiation.
- FDI data valid/ready behavior and stream ID mapping.
- Protocol/flit-format sampling with `pl_protocol_vld`.
- Error containment path through `lp_linkerror`, `pl_error`, `pl_flit_cancel`, and link state transitions.

### Adapter Verification

- Capability advertisement and finalization sideband sequences.
- Retry/CRC enabled and disabled cases.
- NOP flit insertion for multi-protocol multiplexing.
- Stack 0/1 demux and independent LSM behavior.
- Adapter timeout and header log register update.
- Remote mailbox access, credit, completion, and timeout flows.

### Logical PHY Verification

- Byte-to-lane mapping across x64/x32/x16 and degraded widths.
- Lane reversal and repair mapping.
- Data-to-clock training sequences initiated by transmitter and receiver.
- LFSR seed selection and compare modes.
- Link Training state-machine transitions and retrain/error handling.

### Sideband Verification

- Packet encode/decode for register access and messages.
- Credit accounting on FDI, RDI, and remote sideband access.
- Unconditional completion sinking.
- Fatal UIE escalation on sideband parity/control error.
- Outstanding request limits and forward-progress guarantees.

### Compliance Verification

Protocol Layer compliance relies on PCIe/CXL compliance for those protocol layers, while Streaming protocol compliance is outside the UCIe specification because it is vendor/protocol specific. Adapter and PHY compliance require hardware capabilities in the DUT and a Golden Die with appropriate capability coverage. Source: pp. 330-332.

Adapter compliance should cover:

- Test/NOP flit injection.
- CRC error injection.
- Retry flow exercise independent of Protocol Layer enablement.
- Link state request/response injection.
- Retry injection control. Source: pp. 331-332.

PHY compliance should cover:

- Timing margining.
- Voltage margining when supported.
- BER measurement.
- Lane-to-lane skew.
- TX equalization. Source: p. 332.

## Development Risks And Design Guardrails

- Do not mix Protocol Layer feature negotiation with flit format ownership. UCIe intentionally separates protocol features from packetization and flit packing. Source: p. 36.
- Do not assume Raw Format and multi-protocol muxing can always coexist. The spec calls out mutual exclusion for the basic Multi_Protocol_Enable case. Source: pp. 43-44.
- Do not let sideband request paths deadlock behind completions. Completion sinking and request space must be architected up front. Source: pp. 216-219.
- Do not hide link training state inside PHY-only implementation. Adapter, software registers, and FDI/RDI state/status all depend on it. Source: pp. 102-143 and pp. 248-250.
- Do not make compliance a late add-on. UCIe 1.1 explicitly added hardware capabilities to enable compliance; register-visible test and injection hooks are part of the architecture. Source: pp. 24 and 330-332.

## Suggested Knowledge Base Tags

- `ucie_1_1`
- `protocol_layer`
- `d2d_adapter`
- `logical_phy`
- `electrical_layer`
- `sideband`
- `fdi`
- `rdi`
- `flit_format`
- `link_training`
- `retry_crc`
- `multi_protocol`
- `compliance`
- `register_model`
