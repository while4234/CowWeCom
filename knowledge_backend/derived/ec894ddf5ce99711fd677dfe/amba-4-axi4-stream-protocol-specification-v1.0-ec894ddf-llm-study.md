# AMBA 4 AXI4-Stream Protocol Specification v1.0 学习文档（`source_span:78680996b6378ce9813db33a`）

---

## 1. 协议定位

### 1.1 AXI4-Stream 是什么

- AXI4-Stream protocol 被定义为一种标准接口，用于连接希望交换数据的组件；它既可连接一个生成数据的 single master 到一个接收数据的 single slave，也可用于更多 master/slave 组件的连接。`source_span:a544b06620b1a9e1790130ea`
- 协议支持多个 data streams 共享同一组 wires，并允许构建 generic interconnect 来执行 upsizing、downsizing 和 routing 操作。`source_span:a544b06620b1a9e1790130ea`
- AXI4-Stream interface 支持多种 stream types，并定义 Transfers 与 Packets 之间的关联。`source_span:a544b06620b1a9e1790130ea`
- Data Stream 是从一个 source 到一个 destination 的数据传输；它可以是一系列 individual byte transfers，也可以是一系列 grouped in packets 的 byte transfers。`source_span:501ba6e2ab5abb41079399e8`

### 1.2 分组层次：Packet / Frame / Data Stream

| 层次 | 含义 |
|---|---|
| Packet | Packet 是一组通过 AXI4-Stream interface 一起传输的 bytes；它类似 AXI4 burst，并且可以由 single transfer 或 multiple transfers 组成。`source_span:501ba6e2ab5abb41079399e8` |
| Frame | Frame 是 AXI4-Stream 中最高层级的 byte grouping，包含整数个 packets；示例可大到整个 video frame buffer。`source_span:501ba6e2ab5abb41079399e8` |
| Data Stream | Data Stream 是从一个 source 到一个 destination 的数据传输。`source_span:501ba6e2ab5abb41079399e8` |

### 1.3 常见 stream 风格

| Stream 类型 | 关键特征 |
|---|---|
| Byte stream | Byte stream 是 data bytes 和 null bytes 的传输；在每次 TVALID/TREADY handshake 时，可以传输任意数量的 data bytes；null bytes 没有语义，可被插入或移除。`source_span:9f2139e247adb25ae943e0ab` |
| Continuous aligned stream | Continuous aligned stream 是 data bytes 的传输，且每个 packet 没有 position bytes 或 null bytes。`source_span:9f2139e247adb25ae943e0ab` |
| Continuous unaligned stream | Continuous unaligned stream 在每个 packet 的 first data byte 与 last data byte 之间没有 position bytes；它可以在 packet 开头、结尾或两端有任意数量连续 position bytes。`source_span:8efbea929e70fd5629d295fe` |
| Sparse stream | Sparse stream 是 data bytes 和 position bytes 的传输；所有 data bytes 和 position bytes 都必须从 source 到 destination 保持并传输。`source_span:8efbea929e70fd5629d295fe` |

### 1.4 与 AXI4 write data channel 的关键差异

- AXI4 write data channel 不允许 interleaving，而 AXI4-Stream interface 的差异列表中明确包含这一点。`source_span:f264f4aeec9810c1b28bc6cd`
- AXI4-Stream interface 没有定义的或最大 burst/packet length。`source_span:f264f4aeec9810c1b28bc6cd`
- AXI4-Stream interface 允许 data width 为任意整数个 data bytes。`source_span:f264f4aeec9810c1b28bc6cd`
- AXI4-Stream interface 包含 TID 和 TDEST，用于分别指示 source 和 destination。`source_span:f264f4aeec9810c1b28bc6cd`
- AXI4-Stream interface 更精确定义 TUSER sideband signals 的处理。`source_span:f264f4aeec9810c1b28bc6cd`
- AXI4-Stream interface 包含 TKEEP，用于允许 null bytes 的插入和移除。`source_span:f264f4aeec9810c1b28bc6cd`

---

## 2. 信号速查

### 2.1 宽度参数

| 参数 | 含义 |
|---|---|
| n | Data bus width，以 bytes 为单位。`source_span:26fdb40f51a7edd71581a5d7` |
| i | TID width；推荐最大值为 8 bits。`source_span:26fdb40f51a7edd71581a5d7` |
| d | TDEST width；推荐最大值为 4 bits。`source_span:26fdb40f51a7edd71581a5d7` |
| u | TUSER width；推荐 bit 数为 interface byte width 的整数倍。`source_span:26fdb40f51a7edd71581a5d7` |

### 2.2 Interface signals

| Signal | Source | 验证速记 |
|---|---|---|
| ACLK | Clock source | ACLK 是 global clock signal，所有信号在 ACLK rising edge 被采样。`source_span:26fdb40f51a7edd71581a5d7` |
| ARESETn | Reset source | ARESETn 是 global reset signal，并且 active-LOW。`source_span:26fdb40f51a7edd71581a5d7` |
| TVALID | Master | TVALID 表示 master 正在驱动一个 valid transfer；当 TVALID 和 TREADY 同时 asserted 时 transfer 发生。`source_span:26fdb40f51a7edd71581a5d7` |
| TREADY | Slave | TREADY 表示 slave 能在当前 cycle 接收 transfer。`source_span:26fdb40f51a7edd71581a5d7` |
| TDATA[(8n-1):0] | Master | TDATA 是 primary payload，用于承载 interface 上传递的数据；data payload width 是整数个 bytes。`source_span:26fdb40f51a7edd71581a5d7` |
| TSTRB[(n-1):0] | Master | TSTRB 是 byte qualifier，用于指示相关 TDATA byte 被处理为 data byte 还是 position byte。`source_span:26fdb40f51a7edd71581a5d7` |
| TKEEP[(n-1):0] | Master | TKEEP 是 byte qualifier，用于指示相关 byte 的内容是否必须传输到 destination；TKEEP LOW 表示 null byte，可从 stream 中移除。`source_span:c5a6814c37c1af3aec19b853` |
| TLAST | Master | TLAST 表示 packet boundary。`source_span:3725d6ada3cf073a3080194b` |
| TID[(i-1):0] | Master | TID 是 data stream identifier，用于指示不同 data streams。`source_span:3725d6ada3cf073a3080194b` |
| TDEST[(d-1):0] | Master | TDEST 提供 data stream 的 routing information。`source_span:3725d6ada3cf073a3080194b` |
| TUSER[(u-1):0] | Master | TUSER 是 user-defined sideband information，可随 data stream 一起传输。`source_span:3725d6ada3cf073a3080194b` |

### 2.3 Optional / default signaling 速记

| 信号 | Default / Optional 规则 |
|---|---|
| TREADY | TREADY 在特定情况下可以省略，但推荐始终实现；TREADY default value 为 HIGH。`source_span:952f841f23776568322b7b61` |
| TREADY on slave | 如果 slave 总能接收 transfer，则 slave interface 可以省略 TREADY output。`source_span:952f841f23776568322b7b61` |
| TREADY on master | master interface 省略 TREADY input 表示该 master 不能接受 back-pressure，并假定 TREADY 总为 HIGH。`source_span:952f841f23776568322b7b61` |
| TKEEP | 当 TKEEP 不存在时，TKEEP default 为 all bits HIGH。`source_span:3c9b953e481ef619efe39ed1` |
| TSTRB | 当 TSTRB 不存在时，TSTRB = TKEEP。`source_span:3c9b953e481ef619efe39ed1` |
| TSTRB + TKEEP | 当 TSTRB 和 TKEEP 都不存在时，两者 default 为 all bits HIGH。`source_span:3c9b953e481ef619efe39ed1` |
| TLAST | 对没有 packets 或 frames 概念的 data streams，TLAST default value 是 indeterminate，并可选择 fixed LOW、fixed HIGH 或自动生成 pulsed TLAST。`source_span:e9e303bc018f56c2f65fb996` |
| TLAST unsupported | 当组件不支持 TLAST 且 interconnect topology/functionality 未知时，TLAST 必须 default HIGH。`source_span:7b198b439af3955028641ff5` |
| TID/TDEST/TUSER | TID、TDEST 和 TUSER 都是 optional signals；master 不要求支持这些 outputs。`source_span:7b198b439af3955028641ff5` |
| TID/TDEST/TUSER on slave | 带有额外 TID、TDEST、TUSER inputs 的 slave 必须将所有 bits 固定为 LOW。`source_span:7b198b439af3955028641ff5` |
| TDATA | AXI4-Stream interface 允许没有 TDATA payload；若 TDATA 不存在，则要求 TSTRB 不存在。`source_span:7b198b439af3955028641ff5` |
| TDATA absent + TKEEP present | 在没有 TDATA 时，如果 TKEEP 存在，则 TKEEP bit width 用于决定 upsizing 和 downsizing 操作中的正确处理。`source_span:7b198b439af3955028641ff5` |

---

## 3. 握手与传输规则

### 3.1 TVALID / TREADY handshake

- TVALID 和 TREADY handshake 决定 information 何时通过 interface；two-way flow control 允许 master 和 slave 控制 data/control information 的传输速率。`source_span:062458b070ad33c7a823cee0`
- transfer 发生的条件是 TVALID 和 TREADY 都 asserted。`source_span:062458b070ad33c7a823cee0`
- TVALID 可以先于 TREADY asserted，TREADY 可以先于 TVALID asserted，二者也可以在同一个 ACLK cycle asserted。`source_span:062458b070ad33c7a823cee0`
- master 不允许等到 TREADY asserted 后才 assert TVALID；一旦 TVALID asserted，它必须保持 asserted 直到 handshake 发生。`source_span:062458b070ad33c7a823cee0`
- slave 允许等待 TVALID asserted 后再 assert 对应的 TREADY。`source_span:062458b070ad33c7a823cee0`
- 如果 slave 已经 assert TREADY，则允许在 TVALID asserted 之前 deassert TREADY。`source_span:062458b070ad33c7a823cee0`
- 当 TREADY 在 TVALID 之前为 HIGH 时，表示 destination 可以在单个 ACLK cycle 接收 data/control information；transfer 在 master assert TVALID HIGH 时发生。`source_span:d669142de8b06f6249ab4709`
- 当 master assert TVALID HIGH 且 slave 在同一个 ACLK cycle assert TREADY HIGH 时，transfer 在同一个 cycle 发生。`source_span:d669142de8b06f6249ab4709`
- 关于 “TVALID asserted 后、handshake 前 payload/control 是否必须稳定” 的完整可引用规则：来源片段未覆盖；可见片段在 “must remain unchan...” 处截断，不能据此写成完整结论。`source_span:062458b070ad33c7a823cee0`

### 3.2 Clock / Reset

- 每个 component 使用单个 clock signal ACLK。`source_span:f2e93e44dda8c95b80e7e4ce`
- 所有 input signals 在 ACLK rising edge 采样，所有 output signal changes 必须发生在 ACLK rising edge 之后。`source_span:f2e93e44dda8c95b80e7e4ce`
- 协议包含单个 active-LOW reset signal ARESETn。`source_span:f2e93e44dda8c95b80e7e4ce`
- ARESETn 可以异步 assert，但 deassertion 必须在 ACLK rising edge 之后同步发生。`source_span:f2e93e44dda8c95b80e7e4ce`
- reset 期间 TVALID 必须 driven LOW。`source_span:f2e93e44dda8c95b80e7e4ce`
- reset 期间所有其他 signals 可以 driven to any value。`source_span:f2e93e44dda8c95b80e7e4ce`
- master interface 只能在某个 ACLK rising edge 之后开始 drive TVALID，而该 rising edge 之前的 rising edge 上 ARESETn 已经 asserted HIGH。`source_span:f2e93e44dda8c95b80e7e4ce`

### 3.3 TDATA byte location

- 在 data stream 中，data bus 的 low order bytes 是 stream 中更早的 bytes。`source_span:15eff4c751be39ee3d3cdab6`
- 对 fully packed stream，如果 data bus width 为 w bytes，stream 中 byte n 位于 transfer `t = INT(n/w)`。`source_span:15eff4c751be39ee3d3cdab6`
- 同一条件下，byte n 在该 transfer 中的 byte position 为 `b = n - (t * w)`。`source_span:15eff4c751be39ee3d3cdab6`
- byte position b 对应的 TDATA bit slice 是 `TDATA[(8b+7):8b]`。`source_span:15eff4c751be39ee3d3cdab6`

---

## 4. Packet / Byte Qualifier

### 4.1 Byte types

| Byte type | 协议语义 |
|---|---|
| Data byte | Data byte 包含在 source 和 destination 之间传输的 valid information；data byte 必须从 source 传输到 destination。`source_span:a544b06620b1a9e1790130ea` `source_span:15eff4c751be39ee3d3cdab6` |
| Data byte preservation | data bytes 的数量、关联的 TDATA data values，以及 data byte 相对于其他 data bytes 和 position bytes 的 relative position，必须在 source 和 destination 之间保持。`source_span:15eff4c751be39ee3d3cdab6` |
| Position byte | Position byte 表示 stream 中 data bytes 的相对位置，是 placeholder，不包含需要在 source 和 destination 之间传输的相关 data values。`source_span:a544b06620b1a9e1790130ea` |
| Position byte payload | position byte 关联的 data value 不要求在 source 和 destination 之间传输。`source_span:243af64fd89c2d666a847939` |
| Null byte | Null byte 不包含 data information，也不包含 data bytes 相对位置的信息。`source_span:a544b06620b1a9e1790130ea` |
| Null byte handling | Null byte 不包含信息，可以被插入或从 stream 中移除，并且不要求在 source 和 destination 之间传输。`source_span:243af64fd89c2d666a847939` |

### 4.2 TKEEP / TSTRB bit association

- TKEEP 和 TSTRB 的每一 bit 都关联一个 payload byte。`source_span:c5a6814c37c1af3aec19b853`
- `TKEEP[x]` 关联 `TDATA[(8x+7):8x]`。`source_span:c5a6814c37c1af3aec19b853`
- `TSTRB[x]` 关联 `TDATA[(8x+7):8x]`。`source_span:c5a6814c37c1af3aec19b853`

### 4.3 TKEEP qualification

- TKEEP asserted HIGH 表示 associated byte 必须传输到 destination。`source_span:c5a6814c37c1af3aec19b853`
- TKEEP deasserted LOW 表示 null byte，且该 byte 可以从 stream 中移除。`source_span:c5a6814c37c1af3aec19b853`
- 一个 transfer 中所有 TKEEP bits 都 deasserted LOW 是 legal。`source_span:c5a6814c37c1af3aec19b853`
- 一个所有 TKEEP bits 都 deasserted LOW 的 transfer 可以被 suppressed，除非它同时 TLAST asserted HIGH。`source_span:c5a6814c37c1af3aec19b853`

### 4.4 TSTRB qualification

- TSTRB asserted HIGH 表示 associated byte 包含 valid information，并且是 data byte。`source_span:bf4cc717ecf01237466528b5`
- TSTRB deasserted LOW 表示 associated byte 不包含 valid information，并且是 position byte。`source_span:bf4cc717ecf01237466528b5`
- Position byte 用于指示 stream 中 data bytes 的正确 relative position。`source_span:bf4cc717ecf01237466528b5`
- 因为 position byte 关联的数据不是 valid，interconnect 不需要传输 TSTRB LOW 对应 byte 的 TDATA。`source_span:bf4cc717ecf01237466528b5`

### 4.5 TKEEP / TSTRB 组合表

| TKEEP | TSTRB | Data Type | 含义 |
|---|---|---|---|
| HIGH | HIGH | Data byte | associated byte 包含 valid information，必须在 source 和 destination 之间传输。`source_span:6b1e55e88ae6cb52eabd7e33` |
| HIGH | LOW | Position byte | associated byte 表示 stream 中 data bytes 的 relative position，但不包含相关 data values。`source_span:6b1e55e88ae6cb52eabd7e33` |
| LOW | LOW | Null byte | associated byte 不包含 information，可以从 stream 中移除。`source_span:6b1e55e88ae6cb52eabd7e33` |
| LOW | HIGH | Reserved | 该组合 must not be used。`source_span:6b1e55e88ae6cb52eabd7e33` |

### 4.6 Packet boundary / TLAST

- Packet 是一组通过 interface 一起传输的 bytes，且 AXI4-Stream packet 类似 AXI4 burst。`source_span:8ca8e0edc1481195ed0d6330`
- packet transfer 期间需要考虑的 signals 是 TID、TDEST 和 TLAST。`source_span:8ca8e0edc1481195ed0d6330`
- TLAST deasserted 表示可以有另一个 transfer 跟随，因此允许为了 upsizing、downsizing 或 merging 而延迟当前 transfer。`source_span:8ca8e0edc1481195ed0d6330`
- TLAST asserted 可以被 destination 用于指示 packet boundary。`source_span:8ca8e0edc1481195ed0d6330`
- TLAST asserted 表示 shared link 上进行 arbitration change 的有效率位置。`source_span:8ca8e0edc1481195ed0d6330`
- arbitration 不要求只能发生在 TLAST boundary，但 TLAST signaling 可用于通过保持同一 packet 内 transfers 在一起而提升潜在效率。`source_span:8ca8e0edc1481195ed0d6330`
- 协议中没有给出 start-of-packet boundary signal。`source_span:6b1e55e88ae6cb52eabd7e33`
- packet 的 start 被定义为 reset 后某个 TID/TDEST pair 的第一次出现，或任意唯一 TID/TDEST values 的前一 packet 结束后的 first transfer。`source_span:6b1e55e88ae6cb52eabd7e33`
- packet 内所有 bytes 来自同一 source、面向同一 destination，并具有相同 TID 和 TDEST values。`source_span:6b1e55e88ae6cb52eabd7e33`
- 一个 transfer 可以 TLAST asserted 但不包含 data bytes 或 position bytes。`source_span:36eb86332daceae210d72e4f`
- TLAST asserted 且无 data/position bytes 的 transfer 可用于表示 packet 结束、推动 intermediate buffers 中持有的数据通过，或完成 endpoint 期望 packet 末尾 TLAST 的操作。`source_span:36eb86332daceae210d72e4f`

---

## 5. 互连与路由

### 5.1 Interconnect 对 byte 的保持规则

- interconnect 不得修改 stream 中 data bytes 或 position bytes 的数量或 relative position。`source_span:ea671140e430a2f0cc0d90ce`
- interconnect 允许向 stream 插入 null bytes 或从 stream 移除 null bytes。`source_span:ea671140e430a2f0cc0d90ce`
- 插入 null bytes 可能是某些操作所需，例如 upsizing 到更宽 data bus 时，data/position bytes 不足以构成完整 data width transfer。`source_span:ea671140e430a2f0cc0d90ce`
- master 和 slave components 不要求支持 null bytes，因此能够插入 null bytes 的 interconnect 也应能在 stream 到达不支持 null bytes 的 destination 前移除它们。`source_span:ea671140e430a2f0cc0d90ce`

### 5.2 Merging / Packing

- Merging 是把两个不同 transfers 中的 bytes 合并到一个 transfer 的过程。`source_span:93f41429c68494624d54d384`
- 只有 TID 和 TDEST identifiers 匹配的 transfers 才能被 merged。`source_span:93f41429c68494624d54d384`
- 如果当前 transfer 以 TLAST 标记，则它不得与后续 transfer 合并。`source_span:93f41429c68494624d54d384`
- merging 必须保持 data bytes 和 position bytes 的正确顺序。`source_span:93f41429c68494624d54d384`
- merging 必须保持 TLAST、TSTRB 和 TUSER 的正确关联。`source_span:93f41429c68494624d54d384`
- partial merging 是允许的。`source_span:93f41429c68494624d54d384`
- Packing 是从 stream 中移除 null bytes 的过程。`source_span:93f41429c68494624d54d384`
- 使用 TKEEP associations 的 data stream 可以通过移除 null bytes 来 packed，从而形成更 compressed 的 data stream。`removed_invalid_span:c16cfc626737c10ab9c8358e`
- fully packed data 不是必须的，因此 null bytes 可能在 packing 前后都存在。`removed_invalid_span:c16cfc626737c10ab9c8358e`

### 5.3 Downsizing

- Downsizing 是从给定 data bus width 转换到更窄 data bus width。`removed_invalid_span:c16cfc626737c10ab9c8358e`
- Downsizing 通常会为单个 input transfer 生成多个 output transfers。`removed_invalid_span:c16cfc626737c10ab9c8358e`
- downsizing 输出 stream 中 bytes 的顺序必须与输入 stream 中 bytes 的顺序匹配。`removed_invalid_span:c16cfc626737c10ab9c8358e`
- TSTRB 必须以类似 data 的方式 downsized，以保证 data bytes 和 position bytes 之间的正确关系。`removed_invalid_span:c16cfc626737c10ab9c8358e`
- TLAST 只能关联 downsizing operation 的 final transfer。`removed_invalid_span:c16cfc626737c10ab9c8358e`
- 所有 output transfers 的 TID 和 TDEST 必须匹配 input transfer 的值。`removed_invalid_span:c16cfc626737c10ab9c8358e`
- TUSER information 必须保持与同一个 byte 关联。`removed_invalid_span:c16cfc626737c10ab9c8358e`
- 一个只包含 null bytes、TKEEP deasserted 且 TLAST 未 asserted 的 transfer 可以被 suppressed。`removed_invalid_span:c16cfc626737c10ab9c8358e`

### 5.4 Upsizing

- Upsizing 是从给定 data bus width 转换到更宽 data bus width。`removed_invalid_span:c16cfc626737c10ab9c8358e`
- upsizing 输出 stream 中 bytes 的顺序必须与输入 stream 中 bytes 的顺序匹配。`source_span:362c5c65cb94fd8ef3630c5a`
- TSTRB 必须以类似 data 的方式 upsized，以保证 data bytes 和 position bytes 之间的正确关系。`source_span:362c5c65cb94fd8ef3630c5a`
- TLAST 必须被 preserved。`source_span:362c5c65cb94fd8ef3630c5a`
- 所有 output transactions 的 TID 和 TDEST 必须匹配 input transactions 的值。`source_span:362c5c65cb94fd8ef3630c5a`
- TUSER information 必须保持与同一个 byte 关联。`source_span:362c5c65cb94fd8ef3630c5a`
- 如果没有足够 transfers 来构造 full width upsized stream，则需要 TKEEP signals 来指示 null bytes。`source_span:362c5c65cb94fd8ef3630c5a`

### 5.5 TID / TDEST / Routing

- TID 提供 stream identifier，TDEST 提供 routing information。`source_span:36eb86332daceae210d72e4f`
- 具有相同 TID 和 TDEST values 的 transfers 属于同一 stream。`source_span:7369b3fd042d3b07b67448e9`
- 不允许 merge 属于不同 streams 的 transfers。`source_span:7369b3fd042d3b07b67448e9`
- 不同 streams 的 transfers 允许按 transfer 粒度 interleaving，且不限制在 TLAST boundaries。`source_span:7369b3fd042d3b07b67448e9`
- interconnect 可以生成额外 TID signals，以区分两个 otherwise identical streams。`source_span:7369b3fd042d3b07b67448e9`
- interconnect 可以生成或修改 TDEST signals，用于提供 stream 的 routing information。`source_span:7369b3fd042d3b07b67448e9`
- 对 TID 或 TDEST 的任何修改都必须保证两个不同 streams 保持 unique。`source_span:7369b3fd042d3b07b67448e9`
- 一个常见 usage model 是 interconnect 基于 incoming stream 的 TID information 来生成 outgoing stream 的 TDEST information。`source_span:7369b3fd042d3b07b67448e9`

### 5.6 Transfer interleaving / ordering

- Transfer interleaving 是把不同 streams 的 transfers 以 transfer-by-transfer 方式 interleave 的过程。`source_span:af0f7699fae9ad5102879593`
- interconnect 不要求约束 streams 的 interleaving 以避免超过 slave capability。`source_span:af0f7699fae9ad5102879593`
- 某些 interconnect topologies 中，transfer interleaving 可能被限制到 packet boundaries，以提高通过 transfer merging 获得效率提升的可能性。`source_span:af0f7699fae9ad5102879593`
- AXI4-Stream protocol 要求所有 transfers 保持 ordered，并且不允许 reordering。`source_span:725261353f79c252875b71c9`
- 不允许 reordering 的优点包括：reordering 不会增加 slave 观察到的 stream interleaving、系统 predictability 提高、可通过观察同一 master 到同一 destination 的 later transfer 到达来判断某个 earlier transfer 已到达、系统复杂度降低。`source_span:725261353f79c252875b71c9`

### 5.7 TUSER across interconnect

- TUSER 是 user-defined sideband information，可用于随 data stream 传输。`source_span:3725d6ada3cf073a3080194b`
- User sideband signaling 可用于 data byte、transfer、packet 或 frame-based information。`source_span:4d8afa02833e7ab11fb29191`
- User signaling 的示例用途包括标记 special data items 的位置或类型、携带 parity/control signals/flags 等 ancillary information，以及标识 packet segments。`source_span:4d8afa02833e7ab11fb29191`
- 协议定义 User signaling 以 byte basis 传输。`source_span:4d8afa02833e7ab11fb29191`
- 推荐但不要求 TUSER bit 数为 interface byte width 的整数倍。`source_span:4d8afa02833e7ab11fb29191`
- 每个 byte 的 User signals 必须在 TUSER 中 packed together in adjacent bits。`source_span:4d8afa02833e7ab11fb29191`
- 若每个 data byte 有 m 个 User signals，interface 总宽度为 n bytes，则总 User bits 为 `u = m * n`。`source_span:4d8afa02833e7ab11fb29191`
- byte x 的 User bits 位于 `TUSER[((x*m)+(m-1)):(x*m)]`。`source_span:4d8afa02833e7ab11fb29191`
- 当 associated TKEEP deasserted LOW 时，TUSER bits 的传输不要求也不保证。`source_span:4d8afa02833e7ab11fb29191`
- TUSER 可用于 convey transfer-based information，但可靠传输此类信息只有在 input/output interconnect data bus width 匹配且 interconnect 中的数据宽度转换不改变 data packing 时才保证。`source_span:09cfe25efede30ce035c7925`
- 当 TUSER bits per byte 不匹配时，padding 或 trimming 通过添加或移除每 byte 的 upper bits 完成，而不是整个 TUSER data 的 upper bits。`source_span:470dbc56811a3be20b6f660d`
- TUSER padding 时，任何 additional bits 必须 fixed LOW。`source_span:470dbc56811a3be20b6f660d`
- interconnect 需要支持的 TUSER bits per byte 定义为 `MIN(MAX[TUSER bits per byte of masters], MAX[TUSER bits per byte of slaves])`。`source_span:6ed86705017ac4afe6fbb4a3`
- TUSER adaptation guidelines 包括：narrower master 需要 zero-pad 到 interconnect port，wider master 需要 trim 到 interconnect port，slave 侧若 narrower 则 trim、若 wider 则 zero-pad。`source_span:6ed86705017ac4afe6fbb4a3` `source_span:d7af90753ac3c155766b8b5c`

---

## 6. 验证关注点

### 6.1 Handshake checks

- 检查 master 不得等待 TREADY asserted 后才 assert TVALID。`source_span:062458b070ad33c7a823cee0`
- 检查 TVALID asserted 后必须保持 asserted 直到 handshake 发生。`source_span:062458b070ad33c7a823cee0`
- 检查 transfer 只在 TVALID 和 TREADY 都 asserted 时发生。`source_span:062458b070ad33c7a823cee0`
- 检查 slave 可以等待 TVALID asserted 后再 assert TREADY。`source_span:062458b070ad33c7a823cee0`
- 检查 slave 在 TVALID asserted 前可以 deassert 已 asserted 的 TREADY。`source_span:062458b070ad33c7a823cee0`
- 关于 TVALID 等待 handshake 期间 payload/control stable 的完整断言条件：来源片段未覆盖。`source_span:062458b070ad33c7a823cee0`

### 6.2 Reset checks

- reset 期间 TVALID 必须为 LOW。`source_span:f2e93e44dda8c95b80e7e4ce`
- reset 期间除 TVALID 外的其他 signals 可以为任意值。`source_span:f2e93e44dda8c95b80e7e4ce`
- ARESETn assert 可异步，deassertion 必须同步到 ACLK rising edge。`source_span:f2e93e44dda8c95b80e7e4ce`
- master interface 只能在 ARESETn 已经 HIGH 的 rising edge 之后的 rising ACLK edge 开始 drive TVALID。`source_span:f2e93e44dda8c95b80e7e4ce`

### 6.3 Byte qualifier checks

- 检查 `TKEEP=LOW, TSTRB=HIGH` 的组合不得出现，因为该组合是 Reserved / Must not be used。`source_span:6b1e55e88ae6cb52eabd7e33`
- 检查 TKEEP LOW 表示 null byte，且该 byte 可从 stream 中移除。`source_span:c5a6814c37c1af3aec19b853`
- 检查 TSTRB LOW 表示 position byte，并且 interconnect 不需要传输该 byte 的 TDATA。`source_span:bf4cc717ecf01237466528b5`
- 检查 data bytes 和 position bytes 的数量与相对位置不得被 interconnect 修改。`source_span:ea671140e430a2f0cc0d90ce`

### 6.4 Packet / TLAST checks

- 检查 packet 内所有 bytes 具有相同 TID 和 TDEST values，并来自同一 source、面向同一 destination。`source_span:6b1e55e88ae6cb52eabd7e33`
- 检查 TLAST asserted 的 transfer 不得与后续 transfer merged。`source_span:93f41429c68494624d54d384`
- 检查属于不同 packets 的 transfers 不允许 merged。`source_span:36eb86332daceae210d72e4f`
- 检查不同 TID 或 TDEST values 的 transfers 永远不允许 merged。`source_span:36eb86332daceae210d72e4f`
- 检查 TLAST asserted 且无 data/position bytes 的 transfer 是允许的，并可用于 packet 结束、buffer draining 或 endpoint completion。`source_span:36eb86332daceae210d72e4f`

### 6.5 Width conversion checks

- downsizing 时，输出 bytes 顺序必须匹配输入 stream bytes 顺序。`removed_invalid_span:c16cfc626737c10ab9c8358e`
- downsizing 时，TSTRB 必须随 data 类似转换，以保持 data bytes 和 position bytes 的关系。`removed_invalid_span:c16cfc626737c10ab9c8358e`
- downsizing 时，TLAST 只能关联 final transfer。`removed_invalid_span:c16cfc626737c10ab9c8358e`
- upsizing 时，TLAST 必须 preserved。`source_span:362c5c65cb94fd8ef3630c5a`
- upsizing / downsizing 时，TID 和 TDEST 必须匹配输入值，TUSER information 必须保持与同一 byte 关联。`removed_invalid_span:c16cfc626737c10ab9c8358e` `source_span:362c5c65cb94fd8ef3630c5a`

### 6.6 Interleaving / ordering checks

- 检查 AXI4-Stream transfers 不得 reordering。`source_span:725261353f79c252875b71c9`
- 如果 slave 只支持 limited interleaving，则 system design 或 higher level control mechanism 需要确保不会超过 slave interleaving capability。`source_span:af0f7699fae9ad5102879593` `source_span:92f06596821956e4bc57f2d0`
- 如果 arbitration 使用 TLAST 影响 selection process，则推荐包含 override mechanism，因为只在 TLAST boundaries 仲裁与 permanently fixed LOW TLAST masters 不兼容。`source_span:af0f7699fae9ad5102879593`

### 6.7 Compatibility checks

- Interface compatibility 不保证两个 components 一定能一起工作，因为 shared data structures 格式等 higher level considerations 也需要考虑。`source_span:8407c514d8c3180547f68ca8`
- direct connection 的 master/slave interface 若要兼容，data width 必须相同；否则需要 interconnect component 提供 data width conversion。`source_span:8407c514d8c3180547f68ca8` `source_span:858abb1e3917eb4301125c0f`
- full compatibility 可通过支持所有 input signals 来保证；output signals 可 optional support，并使用 default values 保证 compatible operation。`source_span:8407c514d8c3180547f68ca8`
- 如果 slave 需要区分多个 streams，则必须支持足够的 TID 和 TDEST inputs。`source_span:858abb1e3917eb4301125c0f`
- slave 不要求同时支持 null bytes 和 position bytes。`source_span:9fc8dc6ad47d35fd7c01975a`
- 如果 slave 不支持 position bytes，推荐将所有 bytes 转换为 data bytes；该方式不支持 partial update，且可能导致被覆盖 data bytes 的 corruption，但可确保所有 data bytes 在 stream 中保持正确位置。`source_span:9fc8dc6ad47d35fd7c01975a`
- 如果 slave 不支持 null bytes，则使用执行 packing 的 component 从 stream 移除 null bytes。`source_span:9fc8dc6ad47d35fd7c01975a`

---

## 7. 微信问答高频问题

### Q1：TVALID 和 TREADY 谁必须先拉高？

A：TVALID 可以先于 TREADY，TREADY 可以先于 TVALID，也可以同 cycle asserted；transfer 只在两者都 asserted 时发生。`source_span:062458b070ad33c7a823cee0`

### Q2：master 能不能等 TREADY=1 再拉 TVALID？

A：不能；master 不允许等待 TREADY asserted 后才 assert TVALID。`source_span:062458b070ad33c7a823cee0`

### Q3：TVALID 拉高后能不能撤掉？

A：一旦 TVALID asserted，它必须保持 asserted 直到 handshake 发生。`source_span:062458b070ad33c7a823cee0`

### Q4：slave 能不能等 TVALID=1 再拉 TREADY？

A：可以；slave 允许等待 TVALID asserted 后再 assert 对应的 TREADY。`source_span:062458b070ad33c7a823cee0`

### Q5：reset 期间 TVALID 要求是什么？

A：reset 期间 TVALID 必须 driven LOW，其他 signals 可以 driven to any value。`source_span:f2e93e44dda8c95b80e7e4ce`

### Q6：`TKEEP=0, TSTRB=1` 合法吗？

A：不合法；该组合是 Reserved，Must not be used。`source_span:6b1e55e88ae6cb52eabd7e33`

### Q7：TKEEP 全 0 的 transfer 合法吗？

A：合法；所有 TKEEP bits deasserted LOW 的 transfer 是 legal，并且除非 TLAST asserted HIGH，否则允许被 suppressed。`source_span:c5a6814c37c1af3aec19b853`

### Q8：Packet start 由哪个信号表示？

A：协议没有给出 start-of-packet boundary signal；packet start 由 reset 后某个 TID/TDEST pair 的第一次出现，或对应 TID/TDEST 前一 packet 结束后的 first transfer 决定。`source_span:6b1e55e88ae6cb52eabd7e33`

### Q9：TLAST 一直 LOW 可以吗？

A：对于没有 packets/frames 概念的 stream，TLAST default 是 indeterminate；fixed LOW 表示所有 transfers 在同一 packet 内，但可能导致 intermittent bursts 中 transfers 被延迟，也可能影响 shared channel 上的 stream interleaving。`source_span:e9e303bc018f56c2f65fb996`

### Q10：TLAST 一直 HIGH 会怎样？

A：fixed HIGH 表示所有 transfers 都是 individual packets，可避免 infrastructure 中的 delay 和 shared channel blocking，但会阻止 masters 使用该默认设置的 streams 上的 merging，并阻止 efficient upsizing。`source_span:e9e303bc018f56c2f65fb996`

### Q11：不同 TID/TDEST 的 transfers 能 merge 吗？

A：不能；不同 TID 或 TDEST values 的 transfers 永远不允许 merged。`source_span:36eb86332daceae210d72e4f`

### Q12：不同 streams 能按 transfer 粒度 interleave 吗？

A：可以；不同 streams 的 transfers 允许 per-transfer interleaving，且不限制在 TLAST boundaries。`source_span:7369b3fd042d3b07b67448e9`

### Q13：interconnect 能改 TID/TDEST 吗？

A：可以；interconnect 可以生成额外 TID signals，也可以生成或修改 TDEST signals，但必须保证不同 streams 保持 unique。`source_span:7369b3fd042d3b07b67448e9`

### Q14：AXI4-Stream 是否允许 transfer reordering？

A：不允许；协议要求所有 transfers 保持 ordered，并且不允许 reordering。`source_span:725261353f79c252875b71c9`

### Q15：TUSER 的 transfer-based 信息跨 width conversion 一定可靠吗？

A：不一定；可靠传输 transfer-based TUSER information 只有在 interconnect input/output data bus width 匹配，且任何 data width conversion 不改变 data packing 时才保证。`source_span:09cfe25efede30ce035c7925`

### Q16：没有 TDATA 的 AXI4-Stream interface 允许吗？

A：允许；但若 TDATA 不存在，则 TSTRB 不得存在。`source_span:7b198b439af3955028641ff5`

### Q17：TVALID asserted 后 payload/control 是否必须 stable？

A：来源片段未覆盖完整可引用规则；可见片段只截断到 “must remain unchan...”，不能从 SOURCE PACKET 写成完整稳定性结论。`source_span:062458b070ad33c7a823cee0`

---

## 8. Source Map

| Source span | 覆盖内容 |
|---|---|
| `source_span:78680996b6378ce9813db33a` | 规范标题为 AMBA 4 AXI4-Stream Protocol，Version 为 1.0。`source_span:78680996b6378ce9813db33a` |
| `source_span:a544b06620b1a9e1790130ea` | 协议定位、single master/slave、multiple streams、shared wires、upsizing/downsizing/routing、byte definitions。`source_span:a544b06620b1a9e1790130ea` |
| `source_span:501ba6e2ab5abb41079399e8` | Packet、Frame、Data Stream 的定义。`source_span:501ba6e2ab5abb41079399e8` |
| `source_span:9f2139e247adb25ae943e0ab` | Byte stream 与 continuous aligned stream 的描述。`source_span:9f2139e247adb25ae943e0ab` |
| `source_span:8efbea929e70fd5629d295fe` | Continuous unaligned stream 与 sparse stream 的描述。`source_span:8efbea929e70fd5629d295fe` |
| `source_span:26fdb40f51a7edd71581a5d7` | signal width 参数、ACLK/ARESETn/TVALID/TREADY/TDATA/TSTRB signal list。`source_span:26fdb40f51a7edd71581a5d7` |
| `source_span:3725d6ada3cf073a3080194b` | TLAST、TID、TDEST、TUSER signal list。`source_span:3725d6ada3cf073a3080194b` |
| `source_span:062458b070ad33c7a823cee0` | TVALID/TREADY handshake 基本规则、master/slave 对 TVALID/TREADY 的约束。`source_span:062458b070ad33c7a823cee0` |
| `source_span:d669142de8b06f6249ab4709` | TREADY before TVALID 与 TVALID/TREADY same-cycle handshake 示例语义。`source_span:d669142de8b06f6249ab4709` |
| `source_span:15eff4c751be39ee3d3cdab6` | TDATA primary payload、byte order、byte location 公式、data byte preservation。`source_span:15eff4c751be39ee3d3cdab6` |
| `source_span:243af64fd89c2d666a847939` | position byte data value 不要求传输、null byte 可插入/移除。`source_span:243af64fd89c2d666a847939` |
| `source_span:ea671140e430a2f0cc0d90ce` | interconnect 对 data/position bytes 的保持规则，以及 null bytes 的插入/移除规则。`source_span:ea671140e430a2f0cc0d90ce` |
| `source_span:93f41429c68494624d54d384` | merging 与 packing 定义及 merging 规则。`source_span:93f41429c68494624d54d384` |
| `removed_invalid_span:c16cfc626737c10ab9c8358e` | packing、downsizing 规则与 suppress null-only transfer 条件。`removed_invalid_span:c16cfc626737c10ab9c8358e` |
| `source_span:362c5c65cb94fd8ef3630c5a` | upsizing 规则、TLAST/TID/TDEST/TUSER 保持要求、TKEEP 指示 null bytes。`source_span:362c5c65cb94fd8ef3630c5a` |
| `source_span:c5a6814c37c1af3aec19b853` | TKEEP/TSTRB byte association、TKEEP qualification。`source_span:c5a6814c37c1af3aec19b853` |
| `source_span:bf4cc717ecf01237466528b5` | TSTRB qualification 与 position byte 的 TDATA 传输要求。`source_span:bf4cc717ecf01237466528b5` |
| `source_span:6b1e55e88ae6cb52eabd7e33` | TKEEP/TSTRB combination table、packet start 定义、packet 内 TID/TDEST 规则。`source_span:6b1e55e88ae6cb52eabd7e33` |
| `source_span:8ca8e0edc1481195ed0d6330` | Packet boundaries、TLAST deassert/assert 的用途、TLAST 与 arbitration 的关系。`source_span:8ca8e0edc1481195ed0d6330` |
| `source_span:36eb86332daceae210d72e4f` | 不同 packet/不同 TID/TDEST 禁止 merging、zero data/position TLAST transfer、TID/TDEST signal 含义。`source_span:36eb86332daceae210d72e4f` |
| `source_span:7369b3fd042d3b07b67448e9` | TID/TDEST 与 stream/routing、interleaving、interconnect 修改 TID/TDEST 的规则。`source_span:7369b3fd042d3b07b67448e9` |
| `source_span:f2e93e44dda8c95b80e7e4ce` | ACLK、ARESETn、reset 期间 TVALID/其他 signals、reset 退出后 TVALID 时序。`source_span:f2e93e44dda8c95b80e7e4ce` |
| `source_span:4d8afa02833e7ab11fb29191` | TUSER 用途、byte-basis 传输、TUSER bit mapping、TKEEP LOW 时 TUSER 不保证。`source_span:4d8afa02833e7ab11fb29191` |
| `source_span:09cfe25efede30ce035c7925` | transfer-based TUSER information 的可靠传输约束。`source_span:09cfe25efede30ce035c7925` |
| `source_span:470dbc56811a3be20b6f660d` | TUSER padding/trimming 规则和 padding LOW 规则。`source_span:470dbc56811a3be20b6f660d` |
| `source_span:6ed86705017ac4afe6fbb4a3` | interconnect TUSER bits per byte 计算公式和 TUSER adaptation guidelines。`source_span:6ed86705017ac4afe6fbb4a3` |
| `source_span:d7af90753ac3c155766b8b5c` | slave 侧 TUSER trim/zero-pad 规则及 interconnect 优化条件。`source_span:d7af90753ac3c155766b8b5c` |
| `source_span:952f841f23776568322b7b61` | Optional TREADY、TREADY default HIGH、slave/master interface considerations。`source_span:952f841f23776568322b7b61` |
| `source_span:3c9b953e481ef619efe39ed1` | TKEEP/TSTRB default value rules。`source_span:3c9b953e481ef619efe39ed1` |
| `source_span:e9e303bc018f56c2f65fb996` | Optional TLAST 的 fixed LOW、fixed HIGH、pulsed TLAST 选项及影响。`source_span:e9e303bc018f56c2f65fb996` |
| `source_span:7b198b439af3955028641ff5` | TLAST unsupported default、optional TID/TDEST/TUSER、optional TDATA。`source_span:7b198b439af3955028641ff5` |
| `source_span:8407c514d8c3180547f68ca8` | Compatibility considerations、input/output support、direct connection compatibility。`source_span:8407c514d8c3180547f68ca8` |
| `source_span:858abb1e3917eb4301125c0f` | slave compatibility、data width matching、TID/TDEST 支持与 interleaving upper-limit。`source_span:858abb1e3917eb4301125c0f` |
| `source_span:9fc8dc6ad47d35fd7c01975a` | slave 对 null/position bytes 的支持建议、interconnect reliable transport 要求。`source_span:9fc8dc6ad47d35fd7c01975a` |
| `source_span:af0f7699fae9ad5102879593` | Transfer interleaving 定义、interconnect 不约束 slave capability、TLAST-boundary arbitration 注意事项。`source_span:af0f7699fae9ad5102879593` |
| `source_span:92f06596821956e4bc57f2d0` | limited interleaving slave 的 system design / higher level control 条件。`source_span:92f06596821956e4bc57f2d0` |
| `source_span:725261353f79c252875b71c9` | Transfer ordering、禁止 reordering 及其优点。`source_span:725261353f79c252875b71c9` |
| `source_span:f264f4aeec9810c1b28bc6cd` | AXI4-Stream 与 AXI4 write data channel 的关键差异。`source_span:f264f4aeec9810c1b28bc6cd` |

## Validation Notes

The generator produced invalid source-span IDs that were removed before indexing: `c16cfc626737c10ab9c8358e`
