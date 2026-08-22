针对 factory-agent 项目的几个技术规范：
有两个方面的设计需要确认：

1. 关于 Log：
我估计你目前应该没有关于 log 的规范。如果没有的话，希望你先输出一份关于 log 的设计方案，讲讲你准备怎么设计。我是希望使用loguru库
2. 关于配置（settings / config）：
这部分你准备怎么做？具体包括：
(a) 服务层面常用的一些环境变量
(b) 大模型相关配置：比如 baseURL、API key、温度、top_p，以及 fallback 机制等，整个 config 你打算怎么设计？
3. trace怎么做，需不需要opentelemetry？如果需要的话，trace的设计方案也请输出一份。