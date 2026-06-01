# RustPricer Code Generation Rules

## Architecture
- Enum + Trait 混合: 内置类型用 enum（编译器穷举 match），扩展用 trait
- 无 Observer/全局可变状态: 所有计算输入是不可变快照
- 返回值: 错误路径走 anyhow::Result，不走 panic/unwrap

## Core Types
- `Date`: 内部类型 `crate::types::Date`，不输出 `chrono::NaiveDate`
- Date 内部是 i32（days since epoch），非 milliseconds
- `DayCounter`: enum（实际/360, 30/360, 实际/365, 实际/实际）
- `Calendar`: enum（目标/伦敦/纽约/东京，无扩展）

## Curve Construction
- `BootstrapEngine` 是纯函数: `Input → [Step] → CurveSet`
- 单曲线/多曲线都是同一套拓扑排序管线
- 输出 `CurveSet` 不可变，所有后续计算只读引用
- Collateral switching: 在 `DiscountCurve` enum 里做 enum-to-enum 路由

## Instrument Pricing
- `Instrument` 是核心 enum: `Swap(p), Fra(p), Deposit(p), FixedBond(p)`
- **Trade-level** pricing 走 enum dispatch（`match self { ... }`），不搞虚函数表
- 新增 instrument 需要: (1) 加 enum variant (2) 加 pricer (3) 加 benchmark test

## IborIndex 规范
- `fn fixing_date(&self, spot: Date) -> Date` — 返回 Date 不返回 Result
- 起息日 = spot + fixing_offset（交易日历调整）
- 利率确定日 = fixing_date（提前 2 个伦敦工作日）
- `BuiltinIndex` enum 实现所有常见 index：USD-LIBOR-3M, EUR-EURIBOR-3M 等

## Naming Conventions
- 文件: `snake_case.rs`
- 类型: `PascalCase`
- 方法: `snake_case`
- 枚举变体: `PascalCase`
- crate 名: `rustpricer-types`, `rustpricer-core`, `rustpricer-pricers`

## Testing
- 单元测试用 `#[cfg(test)] mod tests { ... }` 内联
- QL oracle 测试: `tests/vs_ql/` 目录，cargo test 对比 QL 输出
- 数值容差: `1e-12`
- 每个 `Instrument` enum variant 至少有一个 "从 QL 翻译" 的 test case

## FFI
- FFI 层放 `rustpricer-ffi` crate，只暴露 C ABI 函数
- FFI 函数签名: `extern "C" fn`，返回 `FFIResult<T>`（含错误码+消息）
- 内部逻辑不依赖 FFI，FFI 只是薄包装

## Crate Dependency Direction
- `rustpricer-types` 📌 无依赖（最底层，只含纯数据）
- `rustpricer-core` → `rustpricer-types`
- `rustpricer-pricers` → `rustpricer-core`, `rustpricer-types`
- `rustpricer-ffi` → 上面全部
