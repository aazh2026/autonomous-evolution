"""测试会话的全局设置。

**为什么要在这里把 ``.env`` 关掉。**

``Settings`` 是个 ``BaseSettings``，默认会读**当前目录下的 ``.env``**。
这对应用是对的 —— 开发者配好 ``.env``，应用启动就该按那份配置来。
但对**测试**是错的：测试要的是一份**可复现的、与本机无关的**配置，
而开发者的 ``.env`` 会把本机的东西带进来。

这个坑踩过，而且症状极其难认：

    Value error, 环境 test 禁止使用默认 MinIO 凭证 minioadmin/minioadmin

``.env.example`` 里写着 ``ASES_S3_ACCESS_KEY=minioadmin``（那是本地开发的
样例值），``make ui`` 会自动把它复制成 ``.env``；于是**从这一刻起**，
每一次 ``make test`` 都在这句莫名其妙的校验错误上全军覆没 ——
而错误里没有任何一个字提到 ``.env``。

它不止影响这一处：``.env`` 里的 ``ASES_DATABASE_URL``、``ASES_LOG_LEVEL``
也会跟着进测试进程。**测试结果取决于开发者本机有什么文件**，
这是最难查也最该消除的一类不稳定。

**怎么关。** pydantic-settings 在**每次实例化时**才去读
``model_config["env_file"]``（见 ``pydantic_settings/main.py`` 里
``env_file = _env_file if _env_file != ENV_FILE_SENTINEL else
cls.model_config.get("env_file")``），所以在**模块导入期**把它置成
``None`` 就够了 —— 不用去动环境变量，也不用给每个 ``Settings(...)``
调用点补一个 ``_env_file=None``。

**为什么放在模块层而不是 fixture 里。** 有测试模块在**导入期**就会
import ``apps.api.main``，而那个模块在导入时就会构造 ``Settings``
（并把结果缓存进 ``get_settings()``）。fixture 是在导入**之后**才跑的，
那时候缓存已经填好了 —— 于是 fixture 拦不住它，缓存里仍然是带 ``.env``
的那份配置。模块层的赋值发生在任何测试模块被导入**之前**，正好是那个窗口。

**不影响什么。** 真正的环境变量照旧生效 —— ``ASES_TEST_S3_ACCESS_KEY``
这类 CI 里注入的变量走的是环境变量通道，与 dotenv 是两条独立的路。
只有 ``.env`` **文件**被关掉了，而它本来就不该进测试。
"""

from __future__ import annotations

from infrastructure.config.settings import Settings

# 关掉 dotenv 读取。见上面的说明。
Settings.model_config["env_file"] = None
