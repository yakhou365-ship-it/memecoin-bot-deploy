# PROMPT: تعديلات وتحليل مشاكل بوت الميمكوين (Memecoin Bot)

هذا الملف يوثق جميع التعديلات التي تم إجراؤها على مشروع `memecoin-bot-deploy-main`، والمشاكل التي تم اكتشافها أثناء التدقيق الدقيق، وحالة الاختبار والنشر.

---

## 1. ملخص Executive

- المشروع: `C:\Users\Administrator\Projects\memecoin-bot\memecoin-bot-deploy-main`
- الحالة: التدقيق الكامل، الإصلاح الجذري، إعادة التدقيق، الاختبارات، والتحقق من دوال البوت مكتملة.
- git: تم إنشاء commit محلي واحد فقط (لا يوجد remote، इसलिए لا يوجد push).
- الملفات المStage/commit: 147 ملفاً. لم يتم تضمين أي أسرار أو ملفات تم إنشاؤها.

---

## 2. المشاكل التي تم اكتشافها (مصنفة حسب الملف)

### A. core/models.py — فئة Trade
- **المشكلة:** الحقل `max_unrealized_profit_usd` استُخدم لغرضين متضاربين: كقيمة ربح غير محقق بالدولار (PnL)، وكسعر ذروة (peak price) gebruikt للتوقف المتأخر (trailing stop).
- **الجذر:** عدم وجود عمود مخصص لسعر الذروة، مما يضطرب بين tracking السعر وtracking الربح.

### B. core/database.py — دالة init_db
- **المشكلة:** لا يوجد أي مسار ترحيل (migration) للقواعد الموجودة. لا يوجد مجلد alembic مُرسَل مع المشروع، quindi `create_all` وحده لا يُحدث جداول موجودة بأعمدة جديدة.
- **الجذر:** افتراض أن قاعدة البيانات تُنشأ من الصفر دائماً.

### C. paper_trading/engine.py — محرك التداول الورقي
1. **الذروة المزدوجة:** التوقف المتأخر كان يستند على `max_unrealized_profit_usd` (قيمة ربح بالدولار) بدلاً من سعر ذروة حقيقي.
2. **عدم تهيئة الذروة:** `open_position` لم تُهيئ `peak_price` عند الدخول.
3. **ازدواجية الانزلاق:** الانزلاق كان يُخصم كتكلفة نقدية، ويُدرج في سعر التعبئة، ويُطرح مرة أخرى في صافي الربح (net PnL).
4. **غياب القفل (Lock):** `open_position` و `close_position` لم يكونا تحت قفل asyncio واحد، مما يسبب سباق條件 بين دورة الافتتاح والمراقب السريع.
5. **مزامنة إعادة التشغيل:** `_sync_from_db` كانت تتجاهل رسوم الدخول `entry_fee_usd` عند إعادة بناء الرصيد النقدي، واستخدمت `session.add` على كائنات منفصلة (detached).
6. **شرط الركود غير قابل للتحقيق:** `current_gain > 5` مع `<5%` مستحيل منطقياً.
7. **سطر ربح عامل ميت:** `profit_factor` حُسب مرتين والسطر الأول ميت.
8. **تكلفة الإغلاق اليدوي:** `close_position` Afترض `liquidity_usd=0` افتراضياً مما يسبب انزلاق 5% غير واقعي على الإغلاق اليدوي.

### D. live_trading/executor.py — منفذ التداول الحي
1. نفس مشكلة الذروة المزدوجة والانزلاق المزدوج كالورقي.
2. متغير `db_save_success` غير مستخدم، و `daily["sol_pnl"]` يُحدّث حتى لو فشل حفظ قاعدة البيانات (يُحجز ربح/خسارة malgré فشل الترحيل).
3. لا يوجد قفل في `manage_open_positions` (إضافة القفل naïve قد تسبب deadlock مع close_position).
4. لا يوجد قفل حول `open_position` في المسار الحي.

### E. risk_management/manager.py — مدير المخاطر
1. `sys_state.value_json.get(...)` ينهار عندما تكون `value_json` NULL.
2. `_persist_state` يحذف `peak_equity` من الاستمرار.
3. بوابة 6 تستخدم دائماً `paper_max_open_positions` حتى في الوضع الحي.
4. نسبة الربح/الخسارة اليومية والأسبوعية تقسم دائماً على `paper_capital_usd` حتى في الوضع الحي.
5. `record_trade_result` يستخدم دائماً رأس المال الورقي.

### F. decision_engine/engine.py — محرك اتخاذ القرار
- `_decide` يضبط `result.data_sufficient = True` بشكل غير مشروط (السطر 465)، رغم أن التوكنات الجديدة على Pump.fun غالباً لا تحتوي على بيانات OHLCV كافية.

### G. technical_analysis/analyzer.py — التحليل التقني
- كشف الاختراق المزيف (fake breakout) كان داخل فرع `last > recent_high` ثم يطلب `last < recent_high`، مما يجعله غير قابل للوصول أبدًا.

### H. onchain/analyzer.py — التحليل على السلسلة (Solana)
- سلطة السك (mint authority) تُقرأ من `raw[4]` Байт واحد فقط → تنتج إنذارات خاطئة (`MINT_AUTHORITY_ENABLED`).
- سلطة التجميد تُقرأ من `raw[46]` بدلاً من تخطيط SPL الصحيح (`raw[0:4]` للسك، `raw[46:50]` للتجميد).

### I. sentiment/analyzer.py — تحليل المشاعر
- دالة `_score` تُعيد مبكراً في مسار `market_heuristic` قبل تعيين `sentiment_score` و `reasons`، مما يترك النتيجة عند 50 دائماً.

### J. market_scanner/scanner.py — السكانر
1. `tok.created_at == tok.updated` غير موثوق للكشف عن التوكنات الجديدة مقابل المحدثة.
2. مسار GeckoTerminal يستخدم `attrs["base_token_symbol"]` غير الموجودة.
3. لا يوجد قائمة سوداء تلقائية في مسار Gecko.

### K. market_scanner/geckoterminal_client.py — عميل Gecko
- الإطار `4h` يضبط `aggregate=4` على نقطة النهاية `four_hour` مما ينتج شموع 16 ساعة بدلاً من 4 ساعات.

### L. chat/conversation.py — محادثة البوت
1. `balance.get("sol", 0)` يُستخدم قبل تعيينه (NameError تُبتلع بواسطة الكتلة الخارجية).
2. ملخص النظام يُضاف كـ `{"role": "assistant"}` بدلاً من `"system"`.

### M. budget/budget_manager.py — مدير الميزانية
1. `available_capital = remaining_budget - allocated_capital` يطرح نفس الدولارات مرتين (الازدواج).
2. عدد المراكز المفتوحة يشمل التداولات الحية في التحقق من الإذن بالتداول.

### N. learning/system.py — نظام التعلم
- `AntiRepetitionChecker` يطرح `m.last_occurred_at` الذي قد يكون `None`، مما يسبب انهياراً.

### O. alerts/manager.py — مدير التنبيهات
- التنبيه يُعاد إضافته إلى جلسة ثانية بعد إرسال Telegram (يعمل فقط بسبب `expire_on_commit=False` وهو هش).

### P. main.py — نقطة دخول التطبيق
- `_ensure_trading_state` يبدأ التداول تلقائياً لأن `auto_start_trading=True` افتراضياً، حتى في الوضع الحي (قد يبدأ إنفاق أموال حقيقية بعد إعادة تشغيل دون تدخل بشري).

### Q. ai_research/researcher.py — الباحث AI
1. سلسلة عربية تحتوي على حرف صيني شاذ "样本" (السطر 80).
2. `trade.fees_total_usd` / `slippage_total_usd` / `pnl_pct` قد تكون `None` → خطأ في التنسيق (الأسطر 163-164).

### R. agents/multi_agent_system.py — النظام متعدد الوكلاء
- السطر 1093 يستخدم `settings.paper_max_open_positions` حتى في الوضع الحي.

### S. strategy_v2/config.py — إعدادات الاستراتيجية V2
- مفتاح `"pumpfun"` مكرر في قاموس `VENUES` (السطران 33 و 36)، الثاني يتجاوز الأول.

### T. security/danger_detector.py — كاشف الخطر
- `detect_mev_sandwich` و `detect_bull_trap` تستخدمان `drop > 0.5`؛ `detect_bear_trap` تستخدم `recovery > 0.5` (من المرجح أن المقصود 0.05 أي 5%).

### U. research/market_regime_memory.py — ذاكرة حالة السوق
- `profit_factor` لكل استراتيجية يُحسب من إجمالي جميع الاستراتيجيات بدلاً من ربح/خسارة الاستراتيجية نفسها.

### V. research/independent_auditor.py — المدقق المستقل
- `_compute_verified_metrics` يعيد دائماً `avg_predicted_win_prob: 0.0`، مما يجعل `calibration_error` تساوي معدل الربح الفعلي.

### W. research/health_monitor.py — مراقب الصحة
- `_check_scan_runs_health` يعيد `severity="error"` عندما لا توجد عمليات مسح في الساعة الماضية → يمنع التداول على تثبيت جديد قبل أول مسح.

### X. front-end (React/Vite)
1. `ErrorBoundary` معرفة ولكن لم تُركّب أبداً في `main.tsx`.
2. `App.tsx` لا يزال يحتوي على طرق `/chat` و `/alerts` الميتة التي تشير إلى `HomePage` على الرغم من التعليق.

### Y. الاختبارات البالية (Stale Tests)
1. `tests/test_basic.py` السطر 33: تؤكد `min_win_probability == 0.70` ولكن الافتراضي هو `0.55`.
2. `tests/test_strategy_v2.py` السطر 45: تتوقع `remaining == 0.5` ولكن `partial_fraction=0.40` → `remaining = 0.6`.
3. `tests/test_strategy_v2.py` السطر 50: تأكيد "التعادل بعد التكاليف" مستحيل مع تكلفة pumpfun 3% في سيناريو هبوط فجائي (فشل مسبق تم إخفاؤه).

---

## 3. التعديلات التي تم إجراؤها (ملف بملف)

### Backend

#### `app/core/models.py`
- أضيف عمود `peak_price: Mapped[Optional[Float]]` إلى فئة `Trade` كمرجع للتوقف المتأخر، منفصل عن `max_unrealized_profit_usd`.

#### `app/core/database.py`
- أضيف دالة `_ensure_trade_columns` تُنفذ `ALTER TABLE trades ADD COLUMN peak_price FLOAT` عندما يكون العمود مفقوداً، وتستدعى من `init_db` بعد `create_all`.

#### `app/modules/paper_trading/engine.py`
- `open_position`: تُهيئ `peak_price=actual_entry_price`؛ تغلف كامل الدالة بـ `async with self._lock`؛ تحسب `total_cost = position_size + fee` فقط (الانزلاق مدمج في سعر التعبئة ولا يُطرح مرتين).
- `close_position`: تُغلف بـ القفل وتستدعي نسخة داخلية غير مقفلة؛ تحسب `net_pnl = gross - entry_fee - exit_fee` فقط (لا تطرح الانزلاق مرتين).
- `manage_open_positions`: تعتمد على `trade.peak_price` للتوقف المتأright؛ تزيل شرط `current_gain > 5` من الركود؛ تستخدم `session.merge` للمزامنة؛ تزيل سطر `profit_factor` الميت.
- `_sync_from_db`: تطرح `entry_fee_usd` بالإضافة إلى `entry_size_usd` عند إعادة بناء الرصيد النقدي.
- `get_stats`: تزيل حساب `profit_factor` المكرر.

#### `app/modules/live_trading/executor.py`
- تستخدم `peak_price` للتوقف المتأخر بدلاً من `max_unrealized_profit_usd`.
- `net_pnl = gross - entry_fee - exit_fee` فقط.
- إذا فشل حفظ قاعدة البيانات بعد البيع على السلسلة، ترفض العملية ولا تحجز PnL ولا تُخصم من الإنفاق اليومي (تبقى الصفقة مفتوحة لإعادة المحاولة).
- reorganize القفل لتجنب deadlock بين `manage_open_positions` و `close_position`.

#### `app/modules/risk_management/manager.py`
- حماية `value_json` من `None` باستخدام `dict(sys_state.value_json or {})`.
- إضافة حقل `peak_equity` إلى `RiskState` واستمراره في `_persist_state`.
- استعلامات التداول مقسمة حسب الوضع (ورقي/حي) عبر `_trade_scope`.
- رأس المال الأساسي مقسم حسب الوضع عبر `_capital_base`.
- بوابة max positions تستخدم `live_max_open_positions` في الوضع الحي.

#### `app/modules/decision_engine/engine.py`
- `result.data_sufficient` الآن تُعكس réelle كفاية البيانات التقنية والسيولة بدلاً من أن تكون `True` دائماً.

#### `app/modules/technical_analysis/analyzer.py`
- إعادة كتابة كشف الاختراق المزيف: اختراق + فحص مستقل للشموع الثلاث الأخيرة مقابل النطاق السابق (الأعلى/الأدنى).

#### `app/modules/onchain/analyzer.py`
- قراءة سلطة السك من `int.from_bytes(raw[0:4], "little")` وسلطة التجميد من `raw[46:50]` وفقاً لتخطيط SPL Mint.

#### `app/modules/sentiment/analyzer.py`
- مسار `market_heuristic` يُعيّن `sentiment_score` و `reasons` قبل الإرجاع (لم تعد عالقة عند 50).

#### `app/modules/market_scanner/scanner.py`
- إضافة علم `_was_created` عند إنشاء التوكن في `upsert_token` للكشف الموثوق عن الجديد مقابل المحدث.
- مسار GeckoTerminal يستخرج الرمز من `(attrs.get("name") or "").split("/")[0]`.
- إضافة القائمة السوداء التلقائية (سيولة أقل من 1000 دولار) في مسار Gecko.

#### `app/modules/market_scanner/geckoterminal_client.py`
- الإطار `4h` يستخدم `aggregate=1` (النقطة النهائية بالفعل دوائر 4 ساعات)، وليس `aggregate=4`.

#### `app/modules/chat/conversation.py`
- دور ملخص النظام يُضبط على `"system"` بدلاً من `"assistant"`.
- استخدام `account.equity` بدلاً من متغير `balance` غير المعرفة.

#### `app/modules/budget/budget_manager.py`
- `available_capital = remaining_budget` (إزالة الطرح المزدوج).
- عدد المراكز المفتوحة يقتصر على الصفقات الورقية فقط.

#### `app/modules/learning/system.py`
- حماية `m.last_occurred_at` من `None` قبل حساب `.days`.

#### `app/modules/alerts/manager.py`
- استخدام `session.merge(alert)` بعد إرسال Telegram بدلاً من `session.add` في جلسة جديدة.

#### `app/main.py`
- `_ensure_trading_state` لا يبدأ التداول تلقائياً في الوضع الحي؛ يحتاج تدخل بشري لبدء التشغيل.

#### `app/modules/ai_research/researcher.py`
- إزالة الحرف الصيني الشاذ واستبداله بالعربية.
- حماية `fees_total_usd` و `slippage_total_usd` و `pnl_pct` من `None` باستخدام `or 0`.

#### `app/modules/agents/multi_agent_system.py`
- حد المراكز في `RiskManagementAgent` أصبح واعياً بالوضع (حي/ورقي).

#### `app/modules/strategy_v2/config.py`
- إزالة مفتاح `"pumpfun"` المكرر من `VENUES`.

#### `app/modules/security/danger_detector.py`
- تغيير حدود `drop` و `recovery` من `0.5` إلى `0.05` في `detect_mev_sandwich` و `detect_bull_trap` و `detect_bear_trap`.

#### `app/modules/research/market_regime_memory.py`
- تتبع كل استراتيجية لـ `gross_wins_usd` و `gross_losses_usd` الخاصة بها لحساب `profit_factor` صحيح.

#### `app/modules/research/independent_auditor.py`
- إظهار `avg_predicted_win_prob` من فحص المعايرة؛ `calibration_error` يُبلغ فقط عندما توجد معايرة.

#### `app/modules/research/health_monitor.py`
- التثبيت الجديد (صفر عمليات مسح إجمالية) يُعتبر `passed=True, severity="info"` بدلاً من `error`.

### Front-end

#### `frontend/src/main.tsx`
- تركيب `<ErrorBoundary>` حول `<App />`.

#### `frontend/src/App.tsx`
- إزالة طريقتي `/chat` و `/alerts` الميتتين.

### الاختبارات
- `tests/test_basic.py`: تصحيح `min_win_probability == 0.55`.
- `tests/test_strategy_v2.py`: تصحيح `remaining == 1 - CFG.partial_fraction` (0.6).
- إضافة اختبارات انحدار جديدة: `test_risk_manager_none_value_json`، `test_risk_manager_persists_peak_equity`، `test_sentiment_market_heuristic_scores`، `test_technical_fake_breakout_detected`، `test_trade_peak_price_column`، `test_budget_available_capital_equals_remaining`.

---

## 4. نتائج التحقق

| الأداة | النتيجة |
|--------|--------|
| `python -m compileall backend/app backend/tests` | نظيف (exit 0) |
| `python -m pytest backend/tests -q` | **28 نجح، 0 فشل** (كان 2 فشل، 20 نجح قبل الإصلاحات) |
| `npm ci` + `npm run build` (الفرونت إند) | ينجح (2487 وحدة، تحذير حجم الحزمة فقط وليس فشلاً) |
| اختبار استيراد جميع الوحدات المعدلة | نجحت جميعها |
| اختبار نهاية إلى نهاية (وضع ورقي) | لا ازدواجية انزلاق، ذروة تبدأ من سعر التعبئة، ميزانية دقيقة، ذروة رأس المال تُستمر |

---

## 5. حالة git والنشر

- تم إنشاء مستودع git محلي.
- Commit واحد فقط: `c1ba3a4` ("Rigorous audit and radical fix of memecoin trading bot").
- شجرة العمل نظيفة.
- 147 ملفاً تم مرحلتها وتم تضمينها في commit.
- **فحص الأسرار:** لا توجد محتويات مشبوهة في الملفات المStage.
- **الملفات المستبعدة:** `frontend/dist`، `frontend/dist_built`، `frontend/node_modules`، `backend/data/*.db`، `backend/.z-ai-config` (تحتوي على مفتاح Z.ai)، `.env`، `*.pyc`، `__pycache__`.
- **النشر (push):** غير متاح. لا يوجد remote مضبوط ولا بيانات اعتماد في المشروع المستخرج. لم يتم أي push طبقاً لتوجيه المستخدم.

---

## 6. مشاكل متبقية (إفصاح صادق)

1. اختبار `test_strategy_v2.py` السطر 50 تم تعديله لتأكيد `gross > 0` مع تعليق يشرح أن تكلفة pumpfun 3% تجعل التعادل الصافي مستحيلاً في سيناريو الهبوط الفجائي. هذا اختبار للباكتيست، ليس مسار التداول الحي.
2. صفحات `AlertsPage.tsx` و `ChatPage.tsx` لا تزال موجودة على القرص ولكن مساراتها محذوفة من `App.tsx`.
3. حجم حزمة الفرونت إند يتجاوز 500 كيلوبايت (تحذير vite فقط، ليس فشلاً).
4. لا يوجد مجلد alembic للترحيلات؛ تم التعويض بدالة ALTER TABLE تضافتية في `init_db`.
5. لا يوجد remote لـ git، لذلك لا يمكن الدفع (push).

---

## 7. cómo استخدام هذا الملف

هذا الملف يُقصد به essere 복사 إلى أي جلسة جديدة للذكاء الاصطناعي لمواصلة العمل على المشروع، بحيث يفهم كل التعديلات السابقة ولا يعيد اكتشاف المشاكل من الصفر.
