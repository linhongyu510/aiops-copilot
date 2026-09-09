# JVM GC 停顿过长处理方案

## 识别信号

GC pause、分配速率和应用 P99 同步升高，可能伴随吞吐下降、超时或容器探针失败。

## 排查顺序

1. 区分 Young、Mixed/Old、Full GC 及停顿分布。
2. 查看堆占用、分配速率、晋升失败和 humongous object。
3. 对齐流量、缓存、批处理和版本变更。
4. 检查容器 memory limit、CPU throttling 与 JVM 参数。
5. 必要时在合规前提下保留 heap dump 或 JFR。

## 止损与恢复

降低并发、暂停大对象任务或回滚版本。不要仅通过增大堆掩盖泄漏；容器内需保证堆外与系统空间。恢复后观察多个 GC 周期。

## 升级条件

持续 Full GC、接近 OOM 或需要分析内存转储时升级 JVM 与应用负责人。
