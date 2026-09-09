// AIOps Copilot 前端应用（Agent 控制台版）
class AIOpsCopilotApp {
    constructor() {
        this.apiBaseUrl = `${window.location.origin}/api`;
        this.currentMode = 'quick'; // 'quick' 或 'stream'
        this.sessionId = this.generateSessionId();
        this.isStreaming = false;
        this.isDiagnosing = false;
        this.activeAbortController = null;
        this.operationTimerId = null;
        this.operationStartedAt = 0;
        this.apiKey = this.loadApiKey();
        this.sseCursors = {};
        this.currentChatHistory = [];
        this.chatHistories = this.loadChatHistories();
        this.isCurrentChatFromHistory = false;
        // 流式渲染节流状态
        this.pendingRender = null;
        this.renderScheduled = false;
        // 智能滚动：用户上翻阅读时不强制滚底
        this.userScrolledUp = false;
        // KPI 轮询
        this.kpiTimerId = null;
        // AIOps 结构化诊断状态
        this.aiopsSteps = [];
        this.aiopsStepCursor = 0;

        this.initTheme();
        this.initializeElements();
        this.bindEvents();
        this.updateUI();
        this.initMarkdown();
        this.checkAndSetCentered();
        this.renderChatHistory();
        this.refreshServiceStatus();
        this.startKpiPolling();
    }

    // ===== Markdown =====
    initMarkdown() {
        const checkMarked = () => {
            if (typeof marked !== 'undefined') {
                try {
                    marked.setOptions({ breaks: true, gfm: true, headerIds: false, mangle: false });
                    if (typeof hljs !== 'undefined') {
                        marked.setOptions({
                            highlight: (code, lang) => {
                                if (lang && hljs.getLanguage(lang)) {
                                    try {
                                        return hljs.highlight(code, { language: lang }).value;
                                    } catch (_err) {
                                        return code;
                                    }
                                }
                                return code;
                            }
                        });
                    }
                } catch (e) {
                    console.warn('Markdown 配置失败:', e);
                }
            } else {
                setTimeout(checkMarked, 100);
            }
        };
        checkMarked();
    }

    renderMarkdown(content) {
        if (!content) return '';
        if (typeof marked === 'undefined') {
            return this.escapeHtml(content).replace(/\n/g, '<br>');
        }
        try {
            const html = marked.parse(content);
            if (typeof DOMPurify !== 'undefined') {
                return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
            }
            return this.escapeHtml(content).replace(/\n/g, '<br>');
        } catch (e) {
            console.warn('Markdown 渲染失败:', e);
            return this.escapeHtml(content);
        }
    }

    highlightCodeBlocks(container) {
        if (typeof hljs !== 'undefined' && container) {
            try {
                container.querySelectorAll('pre code').forEach((block) => {
                    if (!block.classList.contains('hljs')) hljs.highlightElement(block);
                });
            } catch (_e) { /* 高亮失败不影响内容展示 */ }
        }
    }

    // ===== 主题 =====
    initTheme() {
        const stored = this.safeStorageGet('localStorage', 'aiopsTheme');
        const theme = stored === 'light' ? 'light' : 'dark';
        this.applyTheme(theme);
    }

    applyTheme(theme) {
        this.theme = theme;
        if (theme === 'light') {
            document.documentElement.setAttribute('data-theme', 'light');
        } else {
            document.documentElement.removeAttribute('data-theme');
        }
        if (this.themeToggleBtn) {
            this.themeToggleBtn.textContent = theme === 'light' ? '☀' : '☾';
            this.themeToggleBtn.title = theme === 'light' ? '切换到深色主题' : '切换到浅色主题';
        }
    }

    toggleTheme() {
        const next = this.theme === 'light' ? 'dark' : 'light';
        this.applyTheme(next);
        this.safeStorageSet('localStorage', 'aiopsTheme', next);
    }

    // ===== 存储（失败时静默降级） =====
    safeStorageGet(store, key) {
        try {
            return window[store].getItem(key) || '';
        } catch (_e) {
            return '';
        }
    }

    safeStorageSet(store, key, value) {
        try {
            window[store].setItem(key, value);
        } catch (_e) { /* 隐私模式等场景下静默降级 */ }
    }

    safeStorageRemove(store, key) {
        try {
            window[store].removeItem(key);
        } catch (_e) { /* 静默降级 */ }
    }

    // ===== DOM =====
    initializeElements() {
        this.sidebar = document.querySelector('.sidebar');
        this.newChatBtn = document.getElementById('newChatBtn');
        this.aiOpsSidebarBtn = document.getElementById('aiOpsSidebarBtn');
        this.ragInspectorBtn = document.getElementById('ragInspectorBtn');

        this.messageInput = document.getElementById('messageInput');
        this.sendButton = document.getElementById('sendButton');
        this.toolsBtn = document.getElementById('toolsBtn');
        this.toolsMenu = document.getElementById('toolsMenu');
        this.uploadFileItem = document.getElementById('uploadFileItem');
        this.modeSelectorBtn = document.getElementById('modeSelectorBtn');
        this.modeDropdown = document.getElementById('modeDropdown');
        this.currentModeText = document.getElementById('currentModeText');
        this.fileInput = document.getElementById('fileInput');
        this.uploadStatus = document.getElementById('uploadStatus');
        this.uploadStatusText = document.getElementById('uploadStatusText');

        this.chatMessages = document.getElementById('chatMessages');
        this.chatContainer = document.querySelector('.chat-container');
        this.welcomeGreeting = document.getElementById('welcomeGreeting');
        this.chatHistoryList = document.getElementById('chatHistoryList');
        this.operationPanel = document.getElementById('operationPanel');
        this.operationTitle = document.getElementById('operationTitle');
        this.operationDetail = document.getElementById('operationDetail');
        this.operationTimer = document.getElementById('operationTimer');
        this.cancelButton = document.getElementById('cancelButton');
        this.connectionChip = document.getElementById('connectionChip');
        this.serviceDetail = document.getElementById('serviceDetail');
        this.mobileMenuBtn = document.getElementById('mobileMenuBtn');
        this.mobileCloseBtn = document.getElementById('mobileCloseBtn');
        this.authConfigBtn = document.getElementById('authConfigBtn');
        this.themeToggleBtn = document.getElementById('themeToggleBtn');

        this.apiStatusValue = document.getElementById('apiStatusValue');
        this.milvusStatusValue = document.getElementById('milvusStatusValue');
        this.toolSuccessValue = document.getElementById('toolSuccessValue');
        this.toolCallsValue = document.getElementById('toolCallsValue');
        this.errorBudgetValue = document.getElementById('errorBudgetValue');
        this.guardStatusValue = document.getElementById('guardStatusValue');

        this.apiKeyModal = document.getElementById('apiKeyModal');
        this.apiKeyInput = document.getElementById('apiKeyInput');
        this.apiKeySave = document.getElementById('apiKeySave');
        this.apiKeyClear = document.getElementById('apiKeyClear');
        this.apiKeyCancel = document.getElementById('apiKeyCancel');
        this.apiKeyMasked = document.getElementById('apiKeyMasked');
        this.ragInspectorModal = document.getElementById('ragInspectorModal');
        this.ragInspectorClose = document.getElementById('ragInspectorClose');
        this.ragInspectorInput = document.getElementById('ragInspectorInput');
        this.ragInspectorSearch = document.getElementById('ragInspectorSearch');
        this.ragInspectorClear = document.getElementById('ragInspectorClear');
        this.ragInspectorResults = document.getElementById('ragInspectorResults');

        // 主题按钮在构造早期已初始化过文案，这里再同步一次
        this.applyTheme(this.theme);
        this.updateAuthButton();
        this.checkAndSetCentered();
    }

    bindEvents() {
        if (this.newChatBtn) {
            this.newChatBtn.addEventListener('click', () => this.newChat());
        }
        if (this.aiOpsSidebarBtn) {
            this.aiOpsSidebarBtn.addEventListener('click', () => this.triggerAIOps());
        }
        if (this.ragInspectorBtn) {
            this.ragInspectorBtn.addEventListener('click', () => this.openRagInspector());
        }
        if (this.ragInspectorClose) {
            this.ragInspectorClose.addEventListener('click', () => this.closeRagInspector());
        }
        if (this.ragInspectorModal) {
            this.ragInspectorModal.addEventListener('click', (e) => {
                if (e.target === this.ragInspectorModal) this.closeRagInspector();
            });
        }
        if (this.ragInspectorSearch) {
            this.ragInspectorSearch.addEventListener('click', () => this.runRagInspector());
        }
        if (this.ragInspectorClear) {
            this.ragInspectorClear.addEventListener('click', () => {
                if (this.ragInspectorResults) {
                    this.ragInspectorResults.innerHTML = '<div class="rag-inspector-empty">等待检索</div>';
                }
            });
        }
        if (this.themeToggleBtn) {
            this.themeToggleBtn.addEventListener('click', () => this.toggleTheme());
        }
        if (this.authConfigBtn) {
            this.authConfigBtn.addEventListener('click', () => this.openApiKeyModal());
        }
        if (this.apiKeySave) {
            this.apiKeySave.addEventListener('click', () => this.saveApiKeyFromModal());
        }
        if (this.apiKeyClear) {
            this.apiKeyClear.addEventListener('click', () => this.clearApiKey());
        }
        if (this.apiKeyCancel) {
            this.apiKeyCancel.addEventListener('click', () => this.closeApiKeyModal());
        }
        if (this.apiKeyModal) {
            this.apiKeyModal.addEventListener('click', (e) => {
                if (e.target === this.apiKeyModal) this.closeApiKeyModal();
            });
        }
        if (this.apiKeyInput) {
            this.apiKeyInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    this.saveApiKeyFromModal();
                }
            });
        }

        // 场景建议卡：点击直接填入并发送
        document.querySelectorAll('[data-prompt]').forEach((card) => {
            card.addEventListener('click', () => {
                if (this.isStreaming || !this.messageInput) return;
                this.messageInput.value = card.dataset.prompt || '';
                if (this.sidebar) this.sidebar.classList.remove('mobile-open');
                void this.sendMessage();
            });
        });

        if (this.cancelButton) {
            this.cancelButton.addEventListener('click', () => this.cancelCurrentOperation());
        }
        if (this.mobileMenuBtn && this.sidebar) {
            this.mobileMenuBtn.addEventListener('click', () => {
                this.sidebar.classList.toggle('mobile-open');
            });
        }
        if (this.mobileCloseBtn && this.sidebar) {
            this.mobileCloseBtn.addEventListener('click', () => {
                this.sidebar.classList.remove('mobile-open');
            });
        }

        if (this.modeSelectorBtn) {
            this.modeSelectorBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleModeDropdown();
            });
        }
        document.querySelectorAll('.dropdown-item').forEach((item) => {
            item.addEventListener('click', () => {
                this.selectMode(item.getAttribute('data-mode'));
                this.closeModeDropdown();
            });
        });
        document.addEventListener('click', (e) => {
            if (this.modeSelectorBtn && this.modeDropdown &&
                !this.modeSelectorBtn.contains(e.target) &&
                !this.modeDropdown.contains(e.target)) {
                this.closeModeDropdown();
            }
        });

        if (this.sendButton) {
            this.sendButton.addEventListener('click', () => this.sendMessage());
        }
        if (this.messageInput) {
            this.messageInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    this.sendMessage();
                }
            });
        }

        document.addEventListener('keydown', (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
                e.preventDefault();
                this.newChat();
            }
            if (e.key === 'Escape') {
                if (this.ragInspectorModal && !this.ragInspectorModal.hidden) {
                    this.closeRagInspector();
                } else if (this.apiKeyModal && !this.apiKeyModal.hidden) {
                    this.closeApiKeyModal();
                } else if (this.isStreaming) {
                    this.cancelCurrentOperation();
                } else if (this.sidebar) {
                    this.sidebar.classList.remove('mobile-open');
                }
            }
        });

        if (this.toolsBtn) {
            this.toolsBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleToolsMenu();
            });
        }
        if (this.uploadFileItem) {
            this.uploadFileItem.addEventListener('click', () => {
                if (this.fileInput) this.fileInput.click();
                this.closeToolsMenu();
            });
        }
        document.addEventListener('click', (e) => {
            if (this.toolsBtn && this.toolsMenu &&
                !this.toolsBtn.contains(e.target) &&
                !this.toolsMenu.contains(e.target)) {
                this.closeToolsMenu();
            }
        });
        if (this.fileInput) {
            this.fileInput.addEventListener('change', (e) => this.handleFileSelect(e));
        }

        // 智能滚动：监听用户上翻
        if (this.chatMessages) {
            this.chatMessages.addEventListener('scroll', () => {
                const el = this.chatMessages;
                this.userScrolledUp = el.scrollHeight - el.scrollTop - el.clientHeight > 80;
            }, { passive: true });
        }

        // 页面隐藏时暂停 KPI 轮询，恢复可见时立即刷新
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                this.stopKpiPolling();
            } else {
                void this.refreshServiceStatus();
                this.startKpiPolling();
            }
        });
    }

    // ===== API Key =====
    loadApiKey() {
        return this.safeStorageGet('sessionStorage', 'aiopsApiKey');
    }

    apiHeaders(extra = {}) {
        const headers = { ...extra };
        if (this.apiKey) headers['X-API-Key'] = this.apiKey;
        return headers;
    }

    generateOperationKey(prefix) {
        const suffix = window.crypto?.randomUUID
            ? window.crypto.randomUUID()
            : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
        return `${prefix}-${suffix}`;
    }

    maskApiKey(key) {
        if (!key) return '未配置（本机模式）';
        if (key.length <= 8) return '••••••••';
        return `${key.slice(0, 4)}…${key.slice(-4)}`;
    }

    updateAuthButton() {
        if (this.authConfigBtn) {
            this.authConfigBtn.textContent = this.apiKey ? '已认证' : '本机模式';
            this.authConfigBtn.classList.toggle('authenticated', Boolean(this.apiKey));
        }
    }

    openApiKeyModal() {
        if (!this.apiKeyModal) return;
        if (this.apiKeyMasked) this.apiKeyMasked.textContent = this.maskApiKey(this.apiKey);
        if (this.apiKeyInput) this.apiKeyInput.value = '';
        this.apiKeyModal.hidden = false;
        if (this.apiKeyInput) this.apiKeyInput.focus();
    }

    closeApiKeyModal() {
        if (this.apiKeyModal) this.apiKeyModal.hidden = true;
    }

    saveApiKeyFromModal() {
        const value = this.apiKeyInput ? this.apiKeyInput.value.trim() : '';
        this.apiKey = value;
        if (value) {
            this.safeStorageSet('sessionStorage', 'aiopsApiKey', value);
        } else {
            this.safeStorageRemove('sessionStorage', 'aiopsApiKey');
        }
        this.updateAuthButton();
        this.closeApiKeyModal();
        this.showNotification(value ? 'API Key 已保存到当前标签页' : '已恢复本机无鉴权模式', 'success');
        void this.refreshServiceStatus();
    }

    clearApiKey() {
        this.apiKey = '';
        this.safeStorageRemove('sessionStorage', 'aiopsApiKey');
        if (this.apiKeyInput) this.apiKeyInput.value = '';
        this.updateAuthButton();
        this.closeApiKeyModal();
        this.showNotification('API Key 已清除，恢复本机模式', 'info');
        void this.refreshServiceStatus();
    }

    // ===== RAG 2.0 检索实验台 =====
    openRagInspector() {
        if (!this.ragInspectorModal) return;
        this.ragInspectorModal.hidden = false;
        if (this.ragInspectorInput) this.ragInspectorInput.focus();
    }

    closeRagInspector() {
        if (this.ragInspectorModal) this.ragInspectorModal.hidden = true;
    }

    async runRagInspector() {
        const query = this.ragInspectorInput ? this.ragInspectorInput.value.trim() : '';
        if (!query || !this.ragInspectorResults || !this.ragInspectorSearch) return;
        this.ragInspectorSearch.disabled = true;
        this.ragInspectorResults.innerHTML = '<div class="rag-inspector-empty">正在执行 8 路召回与精排…</div>';
        try {
            const response = await fetch(`${this.apiBaseUrl}/rag/search`, {
                method: 'POST',
                headers: this.apiHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ query, top_k: 5 })
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const payload = await response.json();
            const trace = payload.trace || {};
            const expansion = trace.expansion || {};
            const alternatives = (expansion.alternative_queries || [])
                .map((item) => `<li>${this.escapeHtml(item)}</li>`).join('');
            const branches = Object.entries(trace.branch_counts || {})
                .map(([name, count]) => {
                    const ids = ((trace.branch_rankings || {})[name] || []).slice(0, 5).join(', ');
                    return `<span>${this.escapeHtml(name)} · ${count}<small>${this.escapeHtml(ids)}</small></span>`;
                }).join('');
            const rrfRanking = (trace.rrf_ranking || []).slice(0, 10).join(' → ');
            const rerankerRanking = (trace.reranker_ranking || []).slice(0, 10).join(' → ');
            const candidates = (payload.candidates || []).map((item, index) => {
                const source = item.metadata?._file_name || item.metadata?.source || '未知来源';
                const score = item.reranker_score == null ? `RRF ${Number(item.rrf_score || 0).toFixed(4)}` : `Rerank ${Number(item.reranker_score).toFixed(4)}`;
                return `<article class="rag-result-card"><header><b>[${index + 1}] ${this.escapeHtml(source)}</b><em>${score}</em></header><p>${this.escapeHtml(item.content || '').slice(0, 500)}</p></article>`;
            }).join('');
            this.ragInspectorResults.innerHTML = `
                <section class="rag-trace-card">
                    <label>REWRITE</label><p>${this.escapeHtml(expansion.rewritten_query || query)}</p>
                    <label>MULTI QUERY</label><ul>${alternatives || '<li>已降级</li>'}</ul>
                    <label>RECALL BRANCHES</label><div class="rag-branch-list">${branches || '<span>无结果</span>'}</div>
                    <label>RRF TOP</label><p>${this.escapeHtml(rrfRanking || '无结果')}</p>
                    <label>RERANK TOP</label><p>${this.escapeHtml(rerankerRanking || '未执行')}</p>
                    <small>RRF 候选 ${trace.rrf_candidates || 0} · 最终 ${trace.final_candidates || 0} · 降级 ${(trace.degradations || []).join(', ') || '无'}</small>
                </section>
                ${candidates || '<div class="rag-inspector-empty">未找到超过相关性阈值的内部证据</div>'}
            `;
        } catch (error) {
            this.ragInspectorResults.innerHTML = `<div class="rag-inspector-empty error">检索失败：${this.escapeHtml(error.message || String(error))}</div>`;
        } finally {
            this.ragInspectorSearch.disabled = false;
        }
    }

    // ===== 错误分级 =====
    describeHttpError(status) {
        if (status === 401 || status === 403) {
            return '鉴权失败（401/403）：请点击顶栏"本机模式"按钮配置有效的 API Key。';
        }
        if (status === 429) {
            return '服务过载（429）：请求并发已达上限，请稍后重试。';
        }
        if (status === 502) {
            return '模型服务异常（502）：上游模型暂时不可用，请稍后重试。';
        }
        return `服务返回错误（HTTP ${status}）。`;
    }

    describeNetworkError(error) {
        if (error && error.httpStatus) return this.describeHttpError(error.httpStatus);
        return '网络连接中断：无法连接到后端服务，请确认服务已启动后重试。';
    }

    httpError(status) {
        const err = new Error(this.describeHttpError(status));
        err.httpStatus = status;
        return err;
    }

    // ===== KPI 加载与轮询 =====
    /**
     * 给 KPI 数值加上可悬停的解释。降级状态必须说明「影响是什么、怎么恢复」，
     * 否则新用户只能看到「异常」但不知道该做什么。
     */
    setKpiHint(element, hint) {
        if (!element) return;
        const card = element.closest('.system-kpi-card') || element;
        card.title = hint;
    }

    async refreshServiceStatus() {
        try {
            const [healthResult, metricsResult] = await Promise.allSettled([
                fetch(`${window.location.origin}/health`, {
                    headers: this.apiHeaders({ 'Accept': 'application/json' })
                }),
                fetch(`${this.apiBaseUrl}/metrics/reliability`, {
                    headers: this.apiHeaders({ 'Accept': 'application/json' })
                })
            ]);

            if (healthResult.status !== 'fulfilled') throw healthResult.reason;

            const response = healthResult.value;
            const payload = await response.json();
            const data = payload.data || {};
            const llm = data.llm || {};
            const provider = llm.provider ? `${llm.provider} · ${llm.model}` : 'LLM';
            const milvusConnected = data.milvus?.status === 'connected';

            if (this.apiStatusValue) {
                this.apiStatusValue.textContent = response.ok ? '在线' : '降级';
                this.setKpiHint(this.apiStatusValue, response.ok
                    ? 'FastAPI 控制面正常响应'
                    : '控制面已启动但某项依赖不可用，诊断仍可继续；详情见 /health');
            }
            if (this.milvusStatusValue) {
                this.milvusStatusValue.textContent = milvusConnected ? '已连接' : '未启用';
                this.setKpiHint(this.milvusStatusValue, milvusConnected
                    ? '向量检索可用，问答会引用知识库来源'
                    : '未连接 Milvus，向量检索不可用。启动方式：docker compose -f vector-database.yml up -d');
            }
            if (this.serviceDetail) {
                this.serviceDetail.textContent = `${milvusConnected ? 'Milvus' : 'Milvus 未启用'} · MCP · ${provider}`;
            }
            if (this.connectionChip && !this.isStreaming) {
                this.connectionChip.classList.toggle('busy', !response.ok);
                this.connectionChip.classList.remove('offline');
                this.connectionChip.innerHTML = `<i></i> ${response.ok ? '系统就绪' : '服务降级'}`;
            }

            if (metricsResult.status === 'fulfilled' && metricsResult.value.ok) {
                const metricsPayload = await metricsResult.value.json();
                const metrics = metricsPayload.data || metricsPayload;
                const aggregate = metrics.aggregate || {};
                const totalCalls = Number(aggregate.total_calls || 0);
                const successRate = Number(aggregate.success_rate || 0);
                if (this.toolCallsValue) {
                    this.toolCallsValue.textContent = String(totalCalls);
                    this.setKpiHint(this.toolCallsValue,
                        '本进程累计的 MCP 与本地工具调用次数，重启后归零');
                }
                if (this.toolSuccessValue) {
                    this.toolSuccessValue.textContent = totalCalls > 0
                        ? `${(successRate * 100).toFixed(1)}%`
                        : '暂无样本';
                    this.setKpiHint(this.toolSuccessValue, totalCalls > 0
                        ? `基于本进程 ${totalCalls} 次工具调用统计`
                        : '尚未发生工具调用，先发起一次诊断即可产生样本');
                }
                if (this.errorBudgetValue) {
                    this.errorBudgetValue.textContent = totalCalls > 0
                        ? `${(Number(metrics.slo?.error_budget_remaining || 0) * 100).toFixed(0)}%`
                        : '暂无样本';
                    this.setKpiHint(this.errorBudgetValue,
                        '相对 99% 目标成功率的剩余错误预算；样本来自当前进程');
                }
                if (this.guardStatusValue) {
                    const guards = metrics.dependency_guards || [];
                    const openCount = guards.filter((guard) => guard.state !== 'closed').length;
                    this.guardStatusValue.textContent = guards.length === 0
                        ? '未采样'
                        : openCount > 0 ? `${openCount} 个熔断` : '全部闭合';
                    this.setKpiHint(this.guardStatusValue, guards.length === 0
                        ? '尚未采样到依赖调用，熔断与舱壁状态待观测'
                        : openCount > 0
                            ? `${openCount} 个依赖处于熔断/半开，相关工具会快速失败而不是拖垮整体`
                            : '全部依赖熔断器闭合，调用链路正常');
                }
            }
        } catch (_error) {
            if (this.apiStatusValue) {
                this.apiStatusValue.textContent = '离线';
                this.setKpiHint(this.apiStatusValue,
                    '无法访问 /health，请确认服务是否在运行：python quickstart.py');
            }
            if (this.milvusStatusValue) this.milvusStatusValue.textContent = '未知';
            if (this.serviceDetail) this.serviceDetail.textContent = '服务状态暂不可用';
            if (this.connectionChip && !this.isStreaming) {
                this.connectionChip.classList.remove('busy');
                this.connectionChip.classList.add('offline');
                this.connectionChip.innerHTML = '<i></i> 连接异常';
            }
        }
    }

    startKpiPolling() {
        this.stopKpiPolling();
        // 诊断运行中缩短轮询间隔，平时 30s
        const interval = this.isDiagnosing ? 10000 : 30000;
        this.kpiTimerId = setInterval(() => {
            if (!document.hidden) void this.refreshServiceStatus();
        }, interval);
    }

    stopKpiPolling() {
        if (this.kpiTimerId) clearInterval(this.kpiTimerId);
        this.kpiTimerId = null;
    }

    // ===== 会话管理 =====
    async clearBackendSession(sessionId) {
        if (!sessionId) return;
        try {
            await fetch('/api/chat/clear', {
                method: 'POST',
                headers: this.apiHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ sessionId })
            });
        } catch (_e) { /* 后端清理失败不阻塞前端新建会话 */ }
    }

    newChat() {
        if (this.isStreaming) {
            this.showNotification('请等待当前对话完成后再新建对话', 'warning');
            return;
        }

        if (this.currentChatHistory.length > 0) {
            if (this.isCurrentChatFromHistory) {
                this.updateCurrentChatHistory();
            } else {
                this.saveCurrentChat();
            }
            // 通知后端清理 checkpoint，避免旧会话上下文泄漏
            void this.clearBackendSession(this.sessionId);
        }

        this.isStreaming = false;
        this.isDiagnosing = false;
        if (this.messageInput) this.messageInput.value = '';
        this.currentChatHistory = [];
        this.isCurrentChatFromHistory = false;
        if (this.chatMessages) this.chatMessages.innerHTML = '';
        this.sessionId = this.generateSessionId();
        this.currentMode = 'quick';
        this.aiopsSteps = [];
        this.aiopsStepCursor = 0;
        this.updateUI();
        this.checkAndSetCentered();
        this.renderChatHistory();
    }

    saveCurrentChat() {
        if (this.currentChatHistory.length === 0) return;
        const existingIndex = this.chatHistories.findIndex(h => h.id === this.sessionId);
        if (existingIndex !== -1) {
            this.updateCurrentChatHistory();
            return;
        }
        const firstUserMessage = this.currentChatHistory.find(msg => msg.type === 'user');
        const title = firstUserMessage
            ? (firstUserMessage.content.substring(0, 30) + (firstUserMessage.content.length > 30 ? '...' : ''))
            : '新对话';
        this.chatHistories.unshift({
            id: this.sessionId,
            title,
            messages: [...this.currentChatHistory],
            createdAt: new Date().toISOString(),
            updatedAt: new Date().toISOString()
        });
        if (this.chatHistories.length > 50) {
            this.chatHistories = this.chatHistories.slice(0, 50);
        }
        this.saveChatHistories();
    }

    updateCurrentChatHistory() {
        if (this.currentChatHistory.length === 0) return;
        const existingIndex = this.chatHistories.findIndex(h => h.id === this.sessionId);
        if (existingIndex === -1) {
            this.saveCurrentChat();
            return;
        }
        const history = this.chatHistories[existingIndex];
        history.messages = [...this.currentChatHistory];
        history.updatedAt = new Date().toISOString();
        const firstUserMessage = this.currentChatHistory.find(msg => msg.type === 'user');
        if (firstUserMessage) {
            const newTitle = firstUserMessage.content.substring(0, 30) + (firstUserMessage.content.length > 30 ? '...' : '');
            if (history.title !== newTitle) history.title = newTitle;
        }
        this.saveChatHistories();
    }

    loadChatHistories() {
        try {
            const stored = this.safeStorageGet('localStorage', 'chatHistories');
            return stored ? JSON.parse(stored) : [];
        } catch (e) {
            console.warn('加载历史对话失败:', e);
            return [];
        }
    }

    saveChatHistories() {
        this.safeStorageSet('localStorage', 'chatHistories', JSON.stringify(this.chatHistories));
    }

    renderChatHistory() {
        if (!this.chatHistoryList) return;
        this.chatHistoryList.innerHTML = '';
        this.chatHistories.forEach((history) => {
            const historyItem = document.createElement('div');
            historyItem.className = 'history-item';
            historyItem.dataset.historyId = history.id;

            const content = document.createElement('div');
            content.className = 'history-item-content';
            const title = document.createElement('span');
            title.className = 'history-item-title';
            title.textContent = history.title;
            content.appendChild(title);

            const deleteBtn = document.createElement('button');
            deleteBtn.className = 'history-item-delete';
            deleteBtn.title = '删除';
            deleteBtn.textContent = '×';
            deleteBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                void this.deleteChatHistory(history.id);
            });

            historyItem.appendChild(content);
            historyItem.appendChild(deleteBtn);
            historyItem.addEventListener('click', (e) => {
                if (!e.target.closest('.history-item-delete')) {
                    void this.loadChatHistory(history.id);
                }
            });
            this.chatHistoryList.appendChild(historyItem);
        });
    }

    async loadChatHistory(historyId) {
        const history = this.chatHistories.find(h => h.id === historyId);
        if (!history || this.isStreaming) return;

        if (this.currentChatHistory.length > 0 && this.sessionId !== historyId) {
            if (this.isCurrentChatFromHistory) {
                this.updateCurrentChatHistory();
            } else {
                this.saveCurrentChat();
            }
        }

        const renderLocal = () => {
            this.sessionId = history.id;
            this.currentChatHistory = [...history.messages];
            this.isCurrentChatFromHistory = true;
            if (this.chatMessages) {
                this.chatMessages.innerHTML = '';
                history.messages.forEach(msg => this.addMessage(msg.type, msg.content, false, false));
            }
        };

        try {
            const response = await fetch(`${this.apiBaseUrl}/chat/session/${historyId}`, {
                headers: this.apiHeaders({ 'Accept': 'application/json' })
            });
            if (response.ok) {
                const data = await response.json();
                const backendHistory = data.history || (data.data && data.data.history) || [];
                this.sessionId = history.id;
                this.isCurrentChatFromHistory = true;
                if (this.chatMessages) {
                    this.chatMessages.innerHTML = '';
                    if (backendHistory.length > 0) {
                        this.currentChatHistory = [];
                        backendHistory.forEach(msg => {
                            const messageType = msg.role === 'user' ? 'user' : 'assistant';
                            this.addMessage(messageType, msg.content, false, false);
                        });
                    } else {
                        this.currentChatHistory = [...history.messages];
                        history.messages.forEach(msg => this.addMessage(msg.type, msg.content, false, false));
                    }
                }
            } else {
                renderLocal();
            }
        } catch (error) {
            console.warn('加载会话历史失败:', error);
            renderLocal();
        }

        this.checkAndSetCentered();
        this.renderChatHistory();
    }

    async deleteChatHistory(historyId) {
        try {
            const response = await fetch(`${this.apiBaseUrl}/chat/clear`, {
                method: 'POST',
                headers: this.apiHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ sessionId: historyId })
            });
            if (!response.ok) throw this.httpError(response.status);
            const result = await response.json();
            if (result.status === 'success' || result.code === 200) {
                this.chatHistories = this.chatHistories.filter(h => h.id !== historyId);
                this.saveChatHistories();
                this.renderChatHistory();
                if (this.sessionId === historyId) {
                    this.currentChatHistory = [];
                    if (this.chatMessages) this.chatMessages.innerHTML = '';
                    this.sessionId = this.generateSessionId();
                    this.checkAndSetCentered();
                }
                this.showNotification('会话已清空', 'success');
            } else {
                throw new Error(result.message || '清空会话失败');
            }
        } catch (error) {
            this.showNotification('删除失败: ' + error.message, 'error');
        }
    }

    // ===== 下拉菜单 =====
    toggleToolsMenu() {
        const wrapper = this.toolsBtn && this.toolsBtn.closest('.tools-btn-wrapper');
        if (wrapper) wrapper.classList.toggle('active');
    }

    closeToolsMenu() {
        const wrapper = this.toolsBtn && this.toolsBtn.closest('.tools-btn-wrapper');
        if (wrapper) wrapper.classList.remove('active');
    }

    toggleModeDropdown() {
        const wrapper = this.modeSelectorBtn && this.modeSelectorBtn.closest('.mode-selector-wrapper');
        if (wrapper) wrapper.classList.toggle('active');
    }

    closeModeDropdown() {
        const wrapper = this.modeSelectorBtn && this.modeSelectorBtn.closest('.mode-selector-wrapper');
        if (wrapper) wrapper.classList.remove('active');
    }

    selectMode(mode) {
        if (this.isStreaming) {
            this.showNotification('请等待当前对话完成后再切换模式', 'warning');
            return;
        }
        this.currentMode = mode;
        this.updateUI();
        this.showNotification(`已切换到${mode === 'stream' ? '流式' : '快速'}模式`, 'info');
    }

    updateUI() {
        if (this.currentModeText) {
            this.currentModeText.textContent = this.currentMode === 'stream' ? '流式' : '快速';
        }
        document.querySelectorAll('.dropdown-item').forEach((item) => {
            item.classList.toggle('active', item.getAttribute('data-mode') === this.currentMode);
        });
        if (this.sendButton) this.sendButton.disabled = this.isStreaming;
        if (this.aiOpsSidebarBtn) this.aiOpsSidebarBtn.disabled = this.isStreaming;
        if (this.messageInput) {
            this.messageInput.disabled = this.isStreaming;
            this.messageInput.placeholder = this.isStreaming
                ? '当前任务执行中，可点击取消后继续…'
                : '描述故障现象、服务名称或告警信息…';
        }
        if (this.connectionChip) {
            this.connectionChip.classList.toggle('busy', this.isStreaming);
            this.connectionChip.classList.remove('offline');
            this.connectionChip.innerHTML = `<i></i> ${this.isStreaming ? '任务执行中' : '系统就绪'}`;
        }
    }

    // ===== 操作进度面板 =====
    startOperation(title, detail) {
        if (this.activeAbortController) this.activeAbortController.abort();
        this.activeAbortController = new AbortController();
        this.operationStartedAt = Date.now();
        this.updateOperation(title, detail);
        if (this.operationPanel) this.operationPanel.classList.add('visible');
        if (this.operationTimerId) clearInterval(this.operationTimerId);
        const updateTimer = () => {
            const elapsed = Math.floor((Date.now() - this.operationStartedAt) / 1000);
            if (this.operationTimer) {
                const minutes = String(Math.floor(elapsed / 60)).padStart(2, '0');
                const seconds = String(elapsed % 60).padStart(2, '0');
                this.operationTimer.textContent = `${minutes}:${seconds}`;
            }
        };
        updateTimer();
        this.operationTimerId = setInterval(updateTimer, 1000);
    }

    updateOperation(title, detail = '') {
        if (this.operationTitle && title) this.operationTitle.textContent = title;
        if (this.operationDetail) this.operationDetail.textContent = detail || '正在等待服务响应…';
    }

    endOperation() {
        if (this.operationTimerId) clearInterval(this.operationTimerId);
        this.operationTimerId = null;
        this.activeAbortController = null;
        if (this.operationPanel) this.operationPanel.classList.remove('visible');
        void this.refreshServiceStatus();
    }

    cancelCurrentOperation() {
        if (!this.activeAbortController) return;
        this.activeAbortController.abort();
        this.isStreaming = false;
        this.isDiagnosing = false;
        this.startKpiPolling();
        this.endOperation();
        this.updateUI();
        this.showNotification('已取消当前任务', 'info');
    }

    generateSessionId() {
        return 'session_' + Math.random().toString(36).slice(2, 11) + '_' + Date.now();
    }

    // ===== 统一 SSE 解析 =====
    // 按 SSE 规范逐行解析：支持多行 data（以 \n 连接）、event、id 与注释行，
    // 空行触发一次事件分发；data 负载按 JSON.parse 解析，转义字符由 JSON 层正确处理。
    async *readSSE(body, operationKey) {
        const reader = body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let dataLines = [];
        let eventName = 'message';
        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                for (const rawLine of lines) {
                    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
                    if (line === '') {
                        if (dataLines.length > 0) {
                            const joined = dataLines.join('\n');
                            dataLines = [];
                            const currentEvent = eventName;
                            eventName = 'message';
                            if (joined === '[DONE]') {
                                yield { type: 'done', data: null };
                                continue;
                            }
                            let parsed = null;
                            try {
                                parsed = JSON.parse(joined);
                            } catch (_e) {
                                parsed = { type: 'content', data: joined };
                            }
                            if (parsed && typeof parsed.type === 'string') {
                                yield parsed;
                            } else {
                                yield { type: currentEvent, data: parsed };
                            }
                        }
                        continue;
                    }
                    if (line.startsWith(':')) continue; // 心跳注释
                    if (line.startsWith('id:')) {
                        const cursor = Number(line.substring(3).trim());
                        if (!Number.isNaN(cursor)) this.sseCursors[operationKey] = cursor;
                        continue;
                    }
                    if (line.startsWith('event:')) {
                        eventName = line.substring(6).trim() || 'message';
                        continue;
                    }
                    if (line.startsWith('data:')) {
                        let chunk = line.substring(5);
                        if (chunk.startsWith(' ')) chunk = chunk.substring(1);
                        dataLines.push(chunk);
                    }
                }
            }
            // 流结束时冲刷残留 data
            if (dataLines.length > 0) {
                const joined = dataLines.join('\n');
                try {
                    const parsed = JSON.parse(joined);
                    if (parsed && typeof parsed.type === 'string') yield parsed;
                } catch (_e) { /* 丢弃不完整负载 */ }
            }
        } finally {
            reader.releaseLock();
        }
    }

    // ===== 发送消息 =====
    async sendMessage() {
        const message = this.messageInput ? this.messageInput.value.trim() : '';
        if (!message) {
            this.showNotification('请输入消息内容', 'warning');
            return;
        }
        if (this.isStreaming) {
            this.showNotification('请等待当前对话完成', 'warning');
            return;
        }

        this.addMessage('user', message);
        if (this.messageInput) this.messageInput.value = '';

        this.isStreaming = true;
        this.startOperation(
            '正在生成回答',
            this.currentMode === 'stream' ? '实时接收模型输出…' : '检索知识并组织答案…'
        );
        this.updateUI();

        try {
            if (this.currentMode === 'quick') {
                await this.sendQuickMessage(message);
            } else {
                await this.sendStreamMessage(message);
            }
        } catch (error) {
            if (error.name === 'AbortError') return;
            console.warn('发送消息失败:', error);
            this.addMessage('assistant', this.describeNetworkError(error));
        } finally {
            this.isStreaming = false;
            this.endOperation();
            this.updateUI();
            if (this.isCurrentChatFromHistory && this.currentChatHistory.length > 0) {
                this.updateCurrentChatHistory();
                this.renderChatHistory();
            }
        }
    }

    async sendQuickMessage(message) {
        const loadingMessage = this.addLoadingMessage('正在思考...');
        try {
            const response = await fetch(`${this.apiBaseUrl}/chat`, {
                method: 'POST',
                headers: this.apiHeaders({ 'Content-Type': 'application/json' }),
                signal: this.activeAbortController?.signal,
                body: JSON.stringify({
                    Id: this.sessionId,
                    Question: message
                })
            });

            if (!response.ok) throw this.httpError(response.status);

            const data = await response.json();
            if (loadingMessage && loadingMessage.parentNode) {
                loadingMessage.parentNode.removeChild(loadingMessage);
            }

            if (data.code === 200 || data.message === 'success') {
                const chatResponse = data.data;
                if (chatResponse && chatResponse.success) {
                    this.addMessage('assistant', chatResponse.answer || '（无回复内容）');
                } else if (chatResponse && chatResponse.errorMessage) {
                    throw new Error(chatResponse.errorMessage);
                } else {
                    this.addMessage('assistant', chatResponse?.answer || chatResponse?.errorMessage || '服务返回了空内容');
                }
            } else {
                throw new Error(data.message || '请求失败');
            }
        } catch (error) {
            if (loadingMessage && loadingMessage.parentNode) {
                loadingMessage.parentNode.removeChild(loadingMessage);
            }
            throw error;
        }
    }

    async sendStreamMessage(message, resumeState = null) {
        const operationKey = resumeState?.operationKey || this.generateOperationKey('chat');
        let assistantMessageElement = resumeState?.assistantMessageElement || null;
        let fullResponse = resumeState?.fullResponse || '';
        const reconnectAttempt = resumeState?.reconnectAttempt || 0;
        try {
            const response = await fetch(`${this.apiBaseUrl}/chat_stream`, {
                method: 'POST',
                headers: this.apiHeaders({
                    'Content-Type': 'application/json',
                    'X-Idempotency-Key': operationKey,
                    'Last-Event-ID': String(this.sseCursors[operationKey] || 0)
                }),
                signal: this.activeAbortController?.signal,
                body: JSON.stringify({
                    Id: this.sessionId,
                    Question: message
                })
            });

            if (!response.ok) throw this.httpError(response.status);

            assistantMessageElement = assistantMessageElement || this.addMessage('assistant', '', true);

            for await (const sseMessage of this.readSSE(response.body, operationKey)) {
                if (sseMessage.type === 'content') {
                    fullResponse += sseMessage.data || '';
                    this.scheduleStreamRender(assistantMessageElement, fullResponse);
                } else if (sseMessage.type === 'debug' || sseMessage.type === 'tool_call') {
                    const label = sseMessage.data?.tool || sseMessage.data?.name || sseMessage.message || sseMessage.data || '';
                    this.updateOperation('正在调用诊断工具', typeof label === 'string' ? label : JSON.stringify(label));
                } else if (sseMessage.type === 'search_results') {
                    this.updateOperation('知识检索完成', '正在组织回答…');
                } else if (sseMessage.type === 'done') {
                    this.handleStreamComplete(assistantMessageElement, fullResponse);
                    return;
                } else if (sseMessage.type === 'error') {
                    throw new Error(sseMessage.data || sseMessage.message || '流式生成失败');
                }
            }
            this.handleStreamComplete(assistantMessageElement, fullResponse);
        } catch (error) {
            if (error.name !== 'AbortError' && reconnectAttempt < 2) {
                this.updateOperation('连接中断，正在重试', `第 ${reconnectAttempt + 1} 次重连…`);
                await new Promise(resolve => setTimeout(resolve, 500 * (reconnectAttempt + 1)));
                return this.sendStreamMessage(message, {
                    operationKey,
                    assistantMessageElement,
                    fullResponse,
                    reconnectAttempt: reconnectAttempt + 1
                });
            }
            throw error;
        }
    }

    // ===== 流式渲染节流（rAF 合帧） =====
    scheduleStreamRender(messageElement, text) {
        this.pendingRender = { messageElement, text };
        if (this.renderScheduled) return;
        this.renderScheduled = true;
        requestAnimationFrame(() => {
            this.renderScheduled = false;
            const pending = this.pendingRender;
            this.pendingRender = null;
            if (!pending || !pending.messageElement) return;
            const messageContent = pending.messageElement.querySelector('.message-content');
            if (messageContent) {
                messageContent.innerHTML = this.renderMarkdown(pending.text);
                this.highlightCodeBlocks(messageContent);
            }
            this.scrollToBottom();
        });
    }

    handleStreamComplete(assistantMessageElement, fullResponse) {
        // 冲刷未执行的节流渲染
        if (this.pendingRender && assistantMessageElement) {
            const messageContent = assistantMessageElement.querySelector('.message-content');
            if (messageContent) {
                messageContent.innerHTML = this.renderMarkdown(this.pendingRender.text);
                this.highlightCodeBlocks(messageContent);
            }
            this.pendingRender = null;
        }
        if (assistantMessageElement) {
            assistantMessageElement.classList.remove('streaming');
            if (!fullResponse) {
                const messageContent = assistantMessageElement.querySelector('.message-content');
                if (messageContent) messageContent.textContent = '（无回复内容）';
            }
        }
        if (fullResponse) {
            this.currentChatHistory.push({
                type: 'assistant',
                content: fullResponse,
                timestamp: new Date().toISOString()
            });
            if (this.isCurrentChatFromHistory) {
                this.updateCurrentChatHistory();
                this.renderChatHistory();
            }
        }
        this.scrollToBottom();
    }

    // ===== 消息渲染 =====
    addMessage(type, content, isStreaming = false, saveToHistory = true) {
        if (!isStreaming && saveToHistory && content) {
            this.currentChatHistory.push({
                type,
                content,
                timestamp: new Date().toISOString()
            });
        }

        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${type}${isStreaming ? ' streaming' : ''}`;

        if (type === 'assistant') {
            const messageAvatar = document.createElement('div');
            messageAvatar.className = 'message-avatar';
            messageAvatar.textContent = 'AI';
            messageDiv.appendChild(messageAvatar);
        }

        const messageContentWrapper = document.createElement('div');
        messageContentWrapper.className = 'message-content-wrapper';
        const messageContent = document.createElement('div');
        messageContent.className = 'message-content';

        if (type === 'assistant' && !isStreaming) {
            messageContent.innerHTML = this.renderMarkdown(content);
            this.highlightCodeBlocks(messageContent);
        } else {
            messageContent.textContent = content;
        }

        messageContentWrapper.appendChild(messageContent);
        messageDiv.appendChild(messageContentWrapper);

        if (this.chatMessages) {
            this.chatMessages.appendChild(messageDiv);
            if (this.chatContainer) this.chatContainer.classList.remove('centered');
            this.scrollToBottom(true);
        }
        return messageDiv;
    }

    addLoadingMessage(content) {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message assistant';

        const messageAvatar = document.createElement('div');
        messageAvatar.className = 'message-avatar';
        messageAvatar.textContent = 'AI';
        messageDiv.appendChild(messageAvatar);

        const messageContentWrapper = document.createElement('div');
        messageContentWrapper.className = 'message-content-wrapper';
        const messageContent = document.createElement('div');
        messageContent.className = 'message-content loading-message-content';

        const textSpan = document.createElement('span');
        textSpan.textContent = content;
        const loadingIcon = document.createElement('span');
        loadingIcon.className = 'loading-spinner-icon';
        loadingIcon.setAttribute('aria-hidden', 'true');

        messageContent.appendChild(textSpan);
        messageContent.appendChild(loadingIcon);
        messageContentWrapper.appendChild(messageContent);
        messageDiv.appendChild(messageContentWrapper);

        if (this.chatMessages) {
            this.chatMessages.appendChild(messageDiv);
            if (this.chatContainer) this.chatContainer.classList.remove('centered');
            this.scrollToBottom(true);
        }
        return messageDiv;
    }

    checkAndSetCentered() {
        if (this.chatMessages && this.chatContainer) {
            const hasMessages = this.chatMessages.querySelectorAll('.message').length > 0;
            this.chatContainer.classList.toggle('centered', !hasMessages);
        }
    }

    scrollToBottom(force = false) {
        if (!this.chatMessages) return;
        if (force) this.userScrolledUp = false;
        if (!this.userScrolledUp) {
            this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
        }
    }

    showNotification(message, type = 'info') {
        const notification = document.createElement('div');
        notification.className = `notification ${type}`;
        notification.textContent = message;
        document.body.appendChild(notification);
        setTimeout(() => {
            notification.classList.add('fade-out');
            setTimeout(() => {
                if (notification.parentNode) notification.parentNode.removeChild(notification);
            }, 300);
        }, 3000);
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // ===== 文件上传 =====
    handleFileSelect(event) {
        const file = event.target.files[0];
        if (!file) return;
        if (!this.validateFileType(file)) {
            this.showNotification('只支持上传 TXT 或 Markdown (.md) 格式的文件', 'error');
            this.fileInput.value = '';
            return;
        }
        void this.uploadFile(file);
    }

    validateFileType(file) {
        const fileName = file.name.toLowerCase();
        return ['.txt', '.md', '.markdown'].some(ext => fileName.endsWith(ext));
    }

    setUploadStatus(state, text) {
        if (!this.uploadStatus) return;
        if (!state) {
            this.uploadStatus.hidden = true;
            return;
        }
        this.uploadStatus.hidden = false;
        this.uploadStatus.classList.remove('error', 'success');
        if (state !== 'uploading') this.uploadStatus.classList.add(state);
        if (this.uploadStatusText) this.uploadStatusText.textContent = text;
    }

    async uploadFile(file) {
        const maxSize = 10 * 1024 * 1024;
        if (file.size > maxSize) {
            this.showNotification('文件大小不能超过10MB', 'error');
            return;
        }

        this.isStreaming = true;
        this.updateUI();
        this.setUploadStatus('uploading', `正在上传 ${file.name}（${this.formatFileSize(file.size)}）…`);

        try {
            const formData = new FormData();
            formData.append('file', file);
            const response = await fetch(`${this.apiBaseUrl}/upload`, {
                method: 'POST',
                headers: this.apiHeaders(),
                body: formData
            });

            if (!response.ok) throw this.httpError(response.status);
            const data = await response.json();

            if ((data.code === 200 || data.message === 'success') && data.data) {
                this.setUploadStatus('success', `${file.name} 已加入知识库`);
                this.addMessage('assistant', `${file.name} 上传到知识库成功`, false, true);
                setTimeout(() => this.setUploadStatus(null), 4000);
            } else {
                throw new Error(data.message || '上传失败');
            }
        } catch (error) {
            console.warn('文件上传失败:', error);
            this.setUploadStatus('error', `上传失败：${this.describeNetworkError(error)}`);
            setTimeout(() => this.setUploadStatus(null), 6000);
        } finally {
            if (this.fileInput) this.fileInput.value = '';
            this.isStreaming = false;
            this.updateUI();
        }
    }

    formatFileSize(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
    }

    // ===== AIOps 自动诊断（结构化渲染） =====
    async triggerAIOps() {
        if (this.isStreaming) {
            this.showNotification('请等待当前操作完成', 'warning');
            return;
        }

        this.newChat();

        const container = this.createAIOpsContainer();

        this.isStreaming = true;
        this.isDiagnosing = true;
        this.startKpiPolling();
        this.startOperation('正在启动自动诊断', '连接知识库、日志、监控和数据库工具…');
        this.updateUI();

        try {
            await this.sendAIOpsRequest(container);
        } catch (error) {
            if (error.name === 'AbortError') {
                this.renderAIOpsError(container, '诊断已由用户取消。');
                return;
            }
            console.warn('智能运维分析失败:', error);
            if (!error.aiopsReported) {
                this.renderAIOpsError(container, this.describeNetworkError(error));
            }
        } finally {
            this.isStreaming = false;
            this.isDiagnosing = false;
            this.startKpiPolling();
            this.endOperation();
            this.updateUI();
        }
    }

    createAIOpsContainer() {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message assistant aiops-message';

        const messageAvatar = document.createElement('div');
        messageAvatar.className = 'message-avatar';
        messageAvatar.textContent = 'AI';
        messageDiv.appendChild(messageAvatar);

        const wrapper = document.createElement('div');
        wrapper.className = 'message-content-wrapper';
        const content = document.createElement('div');
        content.className = 'message-content';
        wrapper.appendChild(content);
        messageDiv.appendChild(wrapper);

        if (this.chatMessages) {
            this.chatMessages.appendChild(messageDiv);
            if (this.chatContainer) this.chatContainer.classList.remove('centered');
            this.scrollToBottom(true);
        }
        this.aiopsSteps = [];
        this.aiopsStepCursor = 0;
        return messageDiv;
    }

    aiopsContentEl(container) {
        return container ? container.querySelector('.message-content') : null;
    }

    renderAIOpsStatus(container, message) {
        const content = this.aiopsContentEl(container);
        if (!content) return;
        let statusLine = content.querySelector('.aiops-status-line');
        if (!statusLine) {
            statusLine = document.createElement('div');
            statusLine.className = 'aiops-status-line';
            const dot = document.createElement('i');
            const text = document.createElement('span');
            statusLine.appendChild(dot);
            statusLine.appendChild(text);
            content.appendChild(statusLine);
        }
        const textSpan = statusLine.querySelector('span');
        if (textSpan) textSpan.textContent = message || '正在处理…';
        this.scrollToBottom();
    }

    renderAIOpsPlan(container, event) {
        const content = this.aiopsContentEl(container);
        if (!content) return;
        const statusLine = content.querySelector('.aiops-status-line');
        if (statusLine) statusLine.remove();

        const steps = Array.isArray(event.plan) ? event.plan : [];
        const card = document.createElement('div');
        card.className = 'aiops-plan-card';

        const head = document.createElement('div');
        head.className = 'aiops-plan-head';
        const headLabel = document.createElement('span');
        headLabel.textContent = event.message || '诊断计划已制定';
        const headCount = document.createElement('b');
        headCount.textContent = `${steps.length} 步`;
        head.appendChild(headLabel);
        head.appendChild(headCount);
        card.appendChild(head);

        const list = document.createElement('div');
        list.className = 'aiops-step-list';
        this.aiopsSteps = steps.map((title, index) => {
            const stepEl = document.createElement('div');
            stepEl.className = `aiops-step ${index === 0 ? 'running' : 'pending'}`;

            const marker = document.createElement('div');
            marker.className = 'aiops-step-marker';
            marker.textContent = String(index + 1);

            const body = document.createElement('div');
            body.className = 'aiops-step-body';
            const titleEl = document.createElement('div');
            titleEl.className = 'aiops-step-title';
            titleEl.textContent = typeof title === 'string' ? title : JSON.stringify(title);
            body.appendChild(titleEl);

            stepEl.appendChild(marker);
            stepEl.appendChild(body);
            list.appendChild(stepEl);
            return { el: stepEl, bodyEl: body, state: index === 0 ? 'running' : 'pending' };
        });
        this.aiopsStepCursor = 0;
        card.appendChild(list);
        content.appendChild(card);
        this.scrollToBottom();
    }

    renderAIOpsStepComplete(container, event) {
        const current = this.aiopsSteps[this.aiopsStepCursor];
        if (current) {
            current.state = 'done';
            current.el.classList.remove('running', 'pending');
            current.el.classList.add('done');
            const marker = current.el.querySelector('.aiops-step-marker');
            if (marker) marker.textContent = '✓';
            const preview = event.result_preview || event.current_step || '';
            if (preview) {
                const evidence = document.createElement('div');
                evidence.className = 'aiops-step-evidence';
                evidence.textContent = String(preview).slice(0, 500);
                current.bodyEl.appendChild(evidence);
            }
            this.aiopsStepCursor += 1;
        }
        const next = this.aiopsSteps[this.aiopsStepCursor];
        if (next && next.state === 'pending') {
            next.state = 'running';
            next.el.classList.remove('pending');
            next.el.classList.add('running');
        }
        this.scrollToBottom();
    }

    renderAIOpsReport(container, event) {
        const content = this.aiopsContentEl(container);
        if (!content) return;
        const statusLine = content.querySelector('.aiops-status-line');
        if (statusLine) statusLine.remove();

        // 移除旧报告卡（重放/重连时避免重复）
        const oldCard = content.querySelector('.aiops-report-card');
        if (oldCard) oldCard.remove();

        const card = document.createElement('div');
        card.className = 'aiops-report-card';
        const head = document.createElement('div');
        head.className = 'aiops-report-head';
        head.textContent = '诊断报告 · DIAGNOSIS REPORT';
        const body = document.createElement('div');
        body.className = 'aiops-report-body';
        body.innerHTML = this.renderMarkdown(event.report || event.message || '（报告内容为空）');
        this.highlightCodeBlocks(body);
        card.appendChild(head);
        card.appendChild(body);
        content.appendChild(card);
        this.scrollToBottom();
    }

    renderAIOpsError(container, message) {
        const content = this.aiopsContentEl(container);
        if (!content) return;
        const statusLine = content.querySelector('.aiops-status-line');
        if (statusLine) statusLine.remove();
        // 将仍在运行的步骤标记为失败
        const running = this.aiopsSteps.find(s => s.state === 'running');
        if (running) {
            running.state = 'failed';
            running.el.classList.remove('running');
            running.el.classList.add('failed');
            const marker = running.el.querySelector('.aiops-step-marker');
            if (marker) marker.textContent = '×';
        }
        const errorCard = document.createElement('div');
        errorCard.className = 'aiops-error-card';
        errorCard.textContent = `诊断中断：${message}`;
        content.appendChild(errorCard);
        this.scrollToBottom();
    }

    finalizeAIOps(container, event) {
        // 汇总为一条可存入本地历史的文本
        const parts = [];
        if (this.aiopsSteps.length > 0) {
            parts.push(`诊断计划（${this.aiopsSteps.length} 步）执行完成。`);
        }
        if (event && event.response) parts.push(event.response);
        const reportCard = container && container.querySelector('.aiops-report-body');
        if (reportCard) parts.push(reportCard.textContent || '');
        const summary = parts.filter(Boolean).join('\n\n') || '自动诊断已完成。';
        this.currentChatHistory.push({
            type: 'assistant',
            content: summary,
            timestamp: new Date().toISOString()
        });
        if (this.isCurrentChatFromHistory) {
            this.updateCurrentChatHistory();
            this.renderChatHistory();
        }
    }

    async sendAIOpsRequest(container, resumeState = null) {
        const operationKey = resumeState?.operationKey || this.generateOperationKey('aiops');
        const reconnectAttempt = resumeState?.reconnectAttempt || 0;
        try {
            const response = await fetch(`${this.apiBaseUrl}/aiops`, {
                method: 'POST',
                headers: this.apiHeaders({
                    'Content-Type': 'application/json',
                    'X-Idempotency-Key': operationKey,
                    'Last-Event-ID': String(this.sseCursors[operationKey] || 0)
                }),
                signal: this.activeAbortController?.signal,
                body: JSON.stringify({
                    session_id: this.sessionId
                })
            });

            if (!response.ok) throw this.httpError(response.status);

            for await (const sseMessage of this.readSSE(response.body, operationKey)) {
                this.trackAIOpsEvent(sseMessage);
                if (sseMessage.type === 'status') {
                    this.renderAIOpsStatus(container, sseMessage.message);
                } else if (sseMessage.type === 'plan') {
                    this.renderAIOpsPlan(container, sseMessage);
                } else if (sseMessage.type === 'step_complete') {
                    this.renderAIOpsStepComplete(container, sseMessage);
                } else if (sseMessage.type === 'report') {
                    this.renderAIOpsReport(container, sseMessage);
                } else if (sseMessage.type === 'complete') {
                    this.finalizeAIOps(container, sseMessage);
                    return;
                } else if (sseMessage.type === 'done') {
                    this.finalizeAIOps(container, null);
                    return;
                } else if (sseMessage.type === 'error') {
                    const msg = sseMessage.message || sseMessage.data || '智能运维分析失败';
                    this.renderAIOpsError(container, msg);
                    const businessError = new Error(msg);
                    businessError.aiopsReported = true; // 业务错误不重连
                    throw businessError;
                }
            }
            this.finalizeAIOps(container, null);
        } catch (error) {
            if (error.name !== 'AbortError' && !error.aiopsReported && reconnectAttempt < 2) {
                this.updateOperation('连接中断，正在重试', `第 ${reconnectAttempt + 1} 次重连…`);
                await new Promise(resolve => setTimeout(resolve, 500 * (reconnectAttempt + 1)));
                return this.sendAIOpsRequest(container, {
                    operationKey,
                    reconnectAttempt: reconnectAttempt + 1
                });
            }
            throw error;
        }
    }

    trackAIOpsEvent(event) {
        const stages = {
            status: ['正在准备诊断', event.message || '正在连接诊断组件…'],
            plan: ['诊断计划已生成', event.message || '即将执行排查步骤…'],
            step_complete: ['正在执行诊断计划', event.message || '已完成一个排查步骤'],
            report: ['正在整理诊断报告', '汇总证据、根因与处置建议…'],
            complete: ['诊断完成', '结果已生成'],
            error: ['诊断失败', event.message || event.data || '请检查服务日志']
        };
        const copy = stages[event?.type];
        if (copy) this.updateOperation(copy[0], copy[1]);
    }
}

// 初始化应用
document.addEventListener('DOMContentLoaded', () => {
    new AIOpsCopilotApp();
});
