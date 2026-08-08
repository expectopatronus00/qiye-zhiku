/**
 * 企业智库 RAG 问答系统 v1.1
 * 多轮对话 + 认证 + 知识库隔离 + 高级 UI + 管理后台（用户/配额/配置）
 */

const API_BASE = '';
let currentConversationId = null;
let currentUser = null;
let auditPage = 1;
let adminUsersPage = 1;
let adminConfig = null;   // 管理台配置缓存
let activeSources = [];   // 当前回答来源（用于引用跳转）

// ========== 主题 ==========

function toggleTheme() {
    const cur = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', cur);
    localStorage.setItem('qz_theme', cur);
    updateThemeIcon();
}

function updateThemeIcon() {
    const icon = document.getElementById('theme-icon');
    if (icon) icon.textContent = document.documentElement.getAttribute('data-theme') === 'light' ? '☾' : '☀';
}

// ========== Agent 模式 (v0.9) ==========

let agentMode = false;

function toggleAgentMode() {
    agentMode = !agentMode;
    const chip = document.getElementById('agent-mode-chip');
    if (chip) chip.classList.toggle('active', agentMode);
}

function isAgentMode() {
    return agentMode;
}

// ========== 认证 ==========

function getToken() {
    return localStorage.getItem('qz_token') || '';
}

function setToken(token) {
    if (token) localStorage.setItem('qz_token', token);
    else localStorage.removeItem('qz_token');
}

// 统一请求封装：自动附加 Authorization
async function apiFetch(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const response = await fetch(`${API_BASE}${path}`, { ...options, headers });

    // 令牌失效 → 回登录页
    if (response.status === 401) {
        setToken(null);
        showAuthOverlay();
        throw new Error('未登录或登录已过期，请重新登录');
    }
    return response;
}

function showAuthOverlay() {
    document.getElementById('auth-overlay').style.display = 'flex';
    document.getElementById('app-main').style.display = 'none';
}

function showApp() {
    document.getElementById('auth-overlay').style.display = 'none';
    document.getElementById('app-main').style.display = 'flex';
}

async function doLogin() {
    const username = document.getElementById('login-username').value.trim();
    const password = document.getElementById('login-password').value;
    const errorEl = document.getElementById('auth-error');
    errorEl.textContent = '';

    if (!username || !password) {
        errorEl.textContent = '请输入用户名和密码';
        return;
    }

    try {
        const response = await fetch(`${API_BASE}/api/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
        });
        if (!response.ok) {
            const err = await response.json();
            errorEl.textContent = err.detail || '登录失败';
            return;
        }
        const data = await response.json();
        setToken(data.token);
        currentUser = data.user;
        document.getElementById('login-password').value = '';
        await initApp();
    } catch (err) {
        errorEl.textContent = '登录失败: ' + err.message;
    }
}

async function logout() {
    try {
        await apiFetch('/api/auth/logout', { method: 'POST' });
    } catch (e) { /* 忽略 */ }
    setToken(null);
    currentUser = null;
    currentConversationId = null;
    showAuthOverlay();
}

// 渲染用户信息（登录后 / 免登录模式共用）
function renderUserInfo(user) {
    currentUser = user;
    const avatar = document.getElementById('user-avatar');
    avatar.textContent = user.display_name ? user.display_name[0] : user.username[0];
    document.getElementById('user-name').textContent =
        user.display_name || user.username;
    const roleEl = document.getElementById('user-role');
    roleEl.textContent = user.is_admin ? '管理员' : '用户';
    roleEl.className = 'user-role ' + (user.is_admin ? 'role-admin' : 'role-user');
    // 管理员显示管理台按钮
    document.getElementById('btn-admin').style.display = user.is_admin ? '' : 'none';
}

// 引导收尾：加载知识库/对话/统计，并按 ?conv= 参数打开指定对话
async function finishBootstrap() {
    await loadCollections();
    await loadConversations();
    await refreshStats();

    // URL ?conv=xxx 直接打开指定对话（刷新/分享恢复）
    const convParam = new URLSearchParams(location.search).get('conv');
    if (convParam) {
        currentConversationId = convParam;
        switchConversation(convParam);
    }
}

// 启动：校验令牌
async function initApp() {
    try {
        const response = await apiFetch('/api/auth/me');
        if (!response.ok) throw new Error('auth failed');
        renderUserInfo(await response.json());
    } catch (err) {
        showAuthOverlay();
        return;
    }

    updateThemeIcon();
    showApp();
    await finishBootstrap();
}

// ========== 知识库 ==========

// 加载知识库列表（按权限过滤，显示属主）
async function loadCollections() {
    try {
        const response = await apiFetch('/api/knowledge/collections');
        if (!response.ok) return;
        const data = await response.json();
        const select = document.getElementById('collection-select');
        const current = select.value;
        select.innerHTML = '';

        if (!data.collections || data.collections.length === 0) {
            const option = document.createElement('option');
            option.value = 'default';
            option.textContent = '默认知识库';
            select.appendChild(option);
            return;
        }

        data.collections.forEach(kb => {
            const option = document.createElement('option');
            option.value = kb.name;
            option.textContent = currentUser.is_admin
                ? `${kb.name} (${kb.owner})`
                : kb.name;
            if (kb.name === current) option.selected = true;
            select.appendChild(option);
        });
    } catch (err) {
        console.error('加载知识库失败:', err);
    }
}

// 创建新知识库（登记当前用户为属主）
async function createCollection() {
    const name = prompt('请输入新知识库名称:');
    if (!name) return;

    try {
        const response = await apiFetch(`/api/knowledge/collections/${encodeURIComponent(name)}`, {
            method: 'POST',
        });
        if (response.ok) {
            await loadCollections();
            alert(`知识库 "${name}" 创建成功`);
        } else {
            const err = await response.json();
            alert('创建失败: ' + (err.detail || ''));
        }
    } catch (err) {
        alert('创建失败: ' + err.message);
    }
}

// 删除知识库（属主/管理员）
async function deleteCollection() {
    const name = document.getElementById('collection-select').value;
    if (!confirm(`确定删除知识库 "${name}" 吗？该操作不可恢复！`)) return;
    try {
        const response = await apiFetch(`/api/knowledge/collections/${encodeURIComponent(name)}`, {
            method: 'DELETE',
        });
        if (response.ok) {
            await loadCollections();
            refreshStats();
            alert('知识库已删除');
        } else {
            const err = await response.json();
            alert('删除失败: ' + (err.detail || ''));
        }
    } catch (err) {
        alert('删除失败: ' + err.message);
    }
}

// ========== 对话管理 ==========

// 加载对话列表
async function loadConversations() {
    try {
        const response = await apiFetch('/api/chat/conversations');
        if (!response.ok) return;
        const conversations = await response.json();
        renderConversationList(conversations);
    } catch (err) {
        console.error('加载对话列表失败:', err);
    }
}

// 渲染对话列表
function renderConversationList(conversations) {
    const list = document.getElementById('conversation-list');
    if (!list) return;

    list.innerHTML = '';
    if (!conversations || conversations.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'conv-empty';
        empty.textContent = '暂无对话，点击 + 新建';
        list.appendChild(empty);
        return;
    }
    conversations.forEach(conv => {
        const item = document.createElement('div');
        item.className = 'conv-item' + (conv.id === currentConversationId ? ' active' : '');
        item.dataset.id = conv.id;
        item.innerHTML = `
            <span class="conv-title">${escapeHtml(conv.title)}</span>
            <span class="conv-meta">${conv.message_count}条</span>
            <button class="conv-delete" onclick="event.stopPropagation(); deleteConversation('${conv.id}')" title="删除">&times;</button>
        `;
        item.addEventListener('click', () => switchConversation(conv.id));
        list.appendChild(item);
    });
}

// 新建对话
async function newConversation() {
    try {
        const collection = document.getElementById('collection-select').value;
        const response = await apiFetch('/api/chat/conversations', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ collection_name: collection }),
        });
        if (!response.ok) {
            const err = await response.json();
            alert('创建对话失败: ' + (err.detail || ''));
            return;
        }
        const data = await response.json();
        currentConversationId = data.id;
        clearChatArea();
        loadConversations();
    } catch (err) {
        console.error('创建对话失败:', err);
    }
}

// 切换对话
async function switchConversation(conversationId) {
    currentConversationId = conversationId;
    clearChatArea();
    loadConversations();

    try {
        const response = await apiFetch(`/api/chat/conversations/${conversationId}`);
        if (!response.ok) {
            currentConversationId = null;
            return;
        }
        const data = await response.json();
        document.getElementById('chat-title').textContent = data.title || '智能问答';

        // 恢复消息
        (data.messages || []).forEach(msg => {
            if (msg.role === 'user') {
                appendUserMessage(msg.content);
            } else if (msg.role === 'assistant') {
                appendAssistantMessage(msg.content, msg.sources || [], msg.tool_steps || [], null, msg.id || '', msg.retrieval_debug || null, msg.entity_hits || []);
            }
        });
    } catch (err) {
        console.error('加载对话失败:', err);
    }
}

// 删除对话
async function deleteConversation(conversationId) {
    if (!confirm('确定删除这个对话吗？')) return;
    try {
        const response = await apiFetch(`/api/chat/conversations/${conversationId}`, {
            method: 'DELETE',
        });
        if (!response.ok) return;

        if (currentConversationId === conversationId) {
            currentConversationId = null;
            clearChatArea();
        }
        loadConversations();
    } catch (err) {
        console.error('删除对话失败:', err);
    }
}

// ========== Mini Markdown 渲染（安全 DOM 构建） ==========

// 行内样式: **粗体** *斜体* `代码` [n]引用 [链接](url)
function renderInline(text) {
    const container = document.createElement('span');
    // 先按引用标记切分，再处理其他行内语法
    const tokens = text.split(/(\[\d+\])/g);
    tokens.forEach(tok => {
        if (/^\[\d+\]$/.test(tok)) {
            const n = tok.slice(1, -1);
            const ref = document.createElement('span');
            ref.className = 'md-ref';
            ref.textContent = n;
            ref.dataset.ref = n;
            ref.title = `查看来源 ${n}`;
            ref.onclick = () => highlightSource(n);
            container.appendChild(ref);
        } else if (tok) {
            container.appendChild(renderInlineInner(tok));
        }
    });
    return container;
}

function renderInlineInner(text) {
    const wrap = document.createElement('span');
    // 代码 `x`
    const parts = text.split(/`([^`]+)`/g);
    parts.forEach((part, i) => {
        if (i % 2 === 1) {
            const code = document.createElement('code');
            code.className = 'md-code';
            code.textContent = part;
            wrap.appendChild(code);
        } else if (part) {
            wrap.appendChild(renderInlineRich(part));
        }
    });
    return wrap;
}

function renderInlineRich(text) {
    const frag = document.createDocumentFragment();
    // 链接 [text](url)
    const linkParts = text.split(/\[([^\]]+)\]\(([^)]+)\)/g);
    for (let i = 0; i < linkParts.length; i += 3) {
        if (linkParts[i]) frag.appendChild(document.createTextNode(linkParts[i]));
        if (i + 2 < linkParts.length && linkParts[i + 1] !== undefined) {
            const a = document.createElement('a');
            a.className = 'md-a';
            a.href = linkParts[i + 2];
            a.target = '_blank';
            a.rel = 'noopener';
            a.textContent = linkParts[i + 1];
            frag.appendChild(a);
        }
    }
    // 粗体/斜体
    const rich = document.createElement('span');
    rich.appendChild(frag);
    // 用正则替换文本节点中的 **bold** 和 *italic*
    const walker = document.createTreeWalker(rich, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);
    textNodes.forEach(node => {
        const segs = node.textContent.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g);
        if (segs.length === 1) return;
        const holder = document.createElement('span');
        segs.forEach(seg => {
            if (/^\*\*[^*]+\*\*$/.test(seg)) {
                const b = document.createElement('strong');
                b.className = 'md-strong';
                b.textContent = seg.slice(2, -2);
                holder.appendChild(b);
            } else if (/^\*[^*]+\*$/.test(seg)) {
                const em = document.createElement('em');
                em.className = 'md-em';
                em.textContent = seg.slice(1, -1);
                holder.appendChild(em);
            } else if (seg) {
                holder.appendChild(document.createTextNode(seg));
            }
        });
        node.replaceWith(holder);
    });
    return rich;
}

// 段落级渲染（返回元素数组）
function renderMarkdownBlocks(mdText) {
    const blocks = [];
    const rawLines = mdText.split('\n');
    const lines = [];
    let codeBuf = null;
    let codeLang = '';

    for (const line of rawLines) {
        if (/^\s*```/.test(line)) {
            if (codeBuf !== null) {
                lines.push({ type: 'code', content: codeBuf.join('\n') });
                codeBuf = null;
            } else {
                codeBuf = [];
                codeLang = line.replace(/^\s*```/, '').trim();
            }
            continue;
        }
        if (codeBuf !== null) {
            codeBuf.push(line);
            continue;
        }
        if (line.trim() === '') {
            lines.push({ type: 'blank' });
        } else if (/^\s*\|.*\|\s*$/.test(line)) {
            lines.push({ type: 'table', content: line });
        } else if (/^\s*>\s?/.test(line)) {
            lines.push({ type: 'quote', content: line.replace(/^\s*>\s?/, '') });
        } else if (/^\s*[-*+]\s+/.test(line)) {
            lines.push({ type: 'li', content: line.replace(/^\s*[-*+]\s+/, '') });
        } else if (/^\s*\d+\.\s+/.test(line)) {
            lines.push({ type: 'oli', content: line.replace(/^\s*\d+\.\s+/, '') });
        } else if (/^#{1,3}\s+/.test(line)) {
            const m = line.match(/^(#{1,3})\s+(.*)/);
            lines.push({ type: 'h' + m[1].length, content: m[2] });
        } else if (/^\s*---+\s*$/.test(line)) {
            lines.push({ type: 'hr' });
        } else {
            lines.push({ type: 'p', content: line });
        }
    }
    if (codeBuf !== null) lines.push({ type: 'code', content: codeBuf.join('\n') });

    // 合并相邻段落
    const merged = [];
    let pBuf = [];
    let listBuf = null; // {type: 'ul'|'ol', items: []}
    let tableBuf = [];
    let quoteBuf = [];

    const flushP = () => {
        if (pBuf.length) {
            merged.push({ type: 'p', content: pBuf.join(' ') });
            pBuf = [];
        }
    };
    const flushList = () => {
        if (listBuf) {
            merged.push(listBuf);
            listBuf = null;
        }
    };
    const flushQuote = () => {
        if (quoteBuf.length) {
            merged.push({ type: 'quote', content: quoteBuf.join('\n') });
            quoteBuf = [];
        }
    };
    const flushTable = () => {
        if (tableBuf.length) {
            merged.push({ type: 'table', rows: tableBuf });
            tableBuf = [];
        }
    };

    for (const ln of lines) {
        if (ln.type === 'p') { flushList(); flushQuote(); flushTable(); pBuf.push(ln.content); }
        else if (ln.type === 'blank') { flushP(); flushList(); flushQuote(); flushTable(); }
        else if (ln.type === 'li') { flushP(); flushQuote(); flushTable(); if (!listBuf || listBuf.type !== 'ul') { flushList(); listBuf = { type: 'ul', items: [] }; } listBuf.items.push(ln.content); }
        else if (ln.type === 'oli') { flushP(); flushQuote(); flushTable(); if (!listBuf || listBuf.type !== 'ol') { flushList(); listBuf = { type: 'ol', items: [] }; } listBuf.items.push(ln.content); }
        else if (ln.type === 'quote') { flushP(); flushList(); flushTable(); quoteBuf.push(ln.content); }
        else if (ln.type === 'table') {
            flushP(); flushList(); flushQuote();
            const cells = ln.content.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
            if (cells.every(c => /^:?-{2,}:?$/.test(c))) continue; // 分隔行
            tableBuf.push(cells);
        }
        else { flushP(); flushList(); flushQuote(); flushTable(); merged.push(ln); }
    }
    flushP(); flushList(); flushQuote(); flushTable();

    // 构建 DOM
    for (const blk of merged) {
        const el = document.createElement(blk.type === 'p' ? 'p' : 'div');
        switch (blk.type) {
            case 'p':
                el.className = 'md-p';
                el.appendChild(renderInline(blk.content));
                break;
            case 'h1': case 'h2': case 'h3':
                el.className = 'md-' + blk.type;
                el.appendChild(renderInline(blk.content));
                break;
            case 'ul': case 'ol':
                el.className = 'md-' + blk.type;
                blk.items.forEach(item => {
                    const li = document.createElement('li');
                    li.appendChild(renderInline(item));
                    el.appendChild(li);
                });
                break;
            case 'quote':
                el.className = 'md-blockquote';
                el.appendChild(renderInline(blk.content));
                break;
            case 'hr':
                el.className = 'md-hr';
                break;
            case 'code':
                el.className = 'md-pre';
                const pre = document.createElement('code');
                pre.textContent = blk.content;
                el.appendChild(pre);
                break;
            case 'table': {
                el.className = 'md-table-wrap';
                const table = document.createElement('table');
                table.className = 'md-table';
                blk.rows.forEach((row, i) => {
                    const tr = document.createElement('tr');
                    row.forEach(cell => {
                        const td = document.createElement(i === 0 ? 'th' : 'td');
                        td.appendChild(renderInline(cell));
                        tr.appendChild(td);
                    });
                    table.appendChild(tr);
                });
                el.appendChild(table);
                break;
            }
        }
        blocks.push(el);
    }
    return blocks;
}

// ========== 消息渲染 ==========

function getMessagesContainer() {
    let inner = document.querySelector('#chat-messages .chat-messages-inner');
    if (!inner) {
        const container = document.getElementById('chat-messages');
        container.innerHTML = '';
        inner = document.createElement('div');
        inner.className = 'chat-messages-inner';
        container.appendChild(inner);
    }
    return inner;
}

function clearChatArea() {
    const container = document.getElementById('chat-messages');
    container.innerHTML = '<div class="chat-messages-inner"><div class="welcome-message"><div class="welcome-logo">智</div><h3>企业智库</h3><p>上传文档到知识库，然后开始提问。</p><p>支持多轮对话 · 来源溯源 · 上下文自动关联</p><div class="welcome-suggestions"><button class="suggestion-chip" onclick="quickAsk(\'知识库里有哪些文档？\')">知识库里有哪些文档？</button><button class="suggestion-chip" onclick="quickAsk(\'总结一下文档的核心内容\')">总结一下文档的核心内容</button><button class="suggestion-chip" onclick="quickAsk(\'文档中的告警阈值是多少？\')">文档中的告警阈值是多少？</button></div></div></div>';
    document.getElementById('chat-title').textContent = '智能问答';
}

function appendUserMessage(content) {
    const inner = getMessagesContainer();
    const welcome = inner.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    const div = document.createElement('div');
    div.className = 'message user';
    const bubble = document.createElement('div');
    bubble.className = 'message-content';
    bubble.textContent = content;
    div.appendChild(bubble);
    inner.appendChild(div);
    scrollToBottom();
    return div;
}

function appendAssistantMessage(content, sources, toolSteps, agentInfo, messageId, retrievalDebug, entityHits) {
    const inner = getMessagesContainer();
    const welcome = inner.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    const div = document.createElement('div');
    div.className = 'message assistant';
    if (agentInfo && agentInfo.fallback) div.classList.add('agent-fallback-msg');

    const bubble = document.createElement('div');
    bubble.className = 'message-content';

    // Markdown 渲染（替换回答中的 [来源: xxx] 标记为纯文本，避免干扰）
    const clean = content.replace(/\[来源:\s*[^\]]*\]/g, '');
    renderMarkdownBlocks(clean).forEach(el => bubble.appendChild(el));
    div.appendChild(bubble);

    // Agent 模式：工具调用推理步骤（可折叠，点击展开结果）
    if (toolSteps && toolSteps.length > 0) {
        const stepsBox = document.createElement('div');
        stepsBox.className = 'agent-steps';

        const header = document.createElement('div');
        header.className = 'agent-steps-header';
        header.innerHTML = `
            <span class="agent-steps-icon">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
            </span>
            <span>推理过程 · ${toolSteps.length} 次工具调用</span>
            ${agentInfo && agentInfo.iterations ? `<span class="agent-iterations">${agentInfo.iterations} 轮推理</span>` : ''}
            <span class="agent-steps-arrow">▾</span>`;
        header.onclick = () => stepsBox.classList.toggle('open');

        const body = document.createElement('div');
        body.className = 'agent-steps-body';
        toolSteps.forEach((step, i) => {
            const item = document.createElement('div');
            item.className = 'agent-step';
            item.dataset.tool = step.tool_name || '';
            const args = step.arguments || {};
            const argsText = Object.keys(args).length
                ? Object.entries(args).map(([k, v]) => {
                    const s = String(v);
                    return `<span class="agent-step-arg">${escapeHtml(k)}=<em>${escapeHtml(s.length > 40 ? s.slice(0, 40) + '…' : s)}</em></span>`;
                }).join(' ')
                : '<span class="agent-step-arg muted">无参数</span>';
            const duration = step.duration_ms != null ? `<span class="agent-step-dur">${step.duration_ms}ms</span>` : '';
            item.innerHTML = `
                <div class="agent-step-head" onclick="this.parentElement.classList.toggle('open')">
                    <span class="agent-step-tool">${escapeHtml(step.tool_name || 'tool')}</span>
                    <span class="agent-step-args">${argsText}</span>
                    ${duration}
                    <span class="agent-steps-arrow">▸</span>
                </div>
                <div class="agent-step-result"><pre>${escapeHtml(String(step.result || '').slice(0, 800))}</pre></div>`;
            body.appendChild(item);
        });

        stepsBox.appendChild(header);
        stepsBox.appendChild(body);
        div.appendChild(stepsBox);
    }

    // 降级提示（模型不支持工具调用时）
    if (agentInfo && agentInfo.fallback) {
        const fb = document.createElement('div');
        fb.className = 'agent-fallback-note';
        fb.innerHTML = `⚠ 当前模型不支持工具调用，已自动降级为普通 RAG 回答${agentInfo.fallback_reason ? `（${escapeHtml(String(agentInfo.fallback_reason).slice(0, 80))}）` : ''}`;
        div.appendChild(fb);
    }

    // 来源文件徽章（Agent 模式）
    if (agentInfo && agentInfo.source_files && agentInfo.source_files.length > 0) {
        const files = document.createElement('div');
        files.className = 'agent-files';
        agentInfo.source_files.forEach(f => {
            const tag = document.createElement('span');
            tag.className = 'agent-file-tag';
            tag.textContent = f;
            tag.title = '推理过程中引用的知识库文档';
            files.appendChild(tag);
        });
        div.appendChild(files);
    }

    // 知识图谱实体命中标签 (v1.6)
    if (entityHits && entityHits.length > 0) {
        const hitsBox = document.createElement('div');
        hitsBox.className = 'graph-hits';
        hitsBox.innerHTML = '<span class="graph-hits-label">图谱实体</span>';
        entityHits.forEach(h => {
            const tag = document.createElement('span');
            tag.className = 'graph-hit-tag';
            tag.textContent = h;
            tag.title = '问题命中知识图谱实体，已注入相关实体上下文';
            hitsBox.appendChild(tag);
        });
        div.appendChild(hitsBox);
    }

    // 来源卡片（编号 + 文件名 + 相关度，点击预览）
    if (sources && sources.length > 0) {
        const cards = document.createElement('div');
        cards.className = 'source-cards';
        sources.forEach((src, i) => {
            const chip = document.createElement('div');
            chip.className = 'source-chip';
            chip.dataset.idx = i + 1;
            chip.title = `来源 ${i + 1}：点击展开片段`;
            const filename = src.metadata?.filename || '未知文档';
            const score = Math.round((src.score || 0) * 100);
            chip.innerHTML = `
                <span class="source-idx">${i + 1}</span>
                <span class="source-name">${escapeHtml(filename)}</span>
                <span class="source-score">${score}%</span>`;
            const preview = document.createElement('div');
            preview.className = 'source-preview';
            preview.textContent = (src.content || '').slice(0, 300);
            chip.appendChild(preview);
            chip.addEventListener('click', () => toggleSourceCard(chip, src));
            cards.appendChild(chip);
        });
        div.appendChild(cards);
    }

    // 检索详情折叠面板（v1.2：召回路径/融合分/耗时诊断）
    if (retrievalDebug && (retrievalDebug.hybrid !== undefined || retrievalDebug.paths)) {
        const dbg = document.createElement('div');
        dbg.className = 'retrieval-debug';
        const dHeader = document.createElement('div');
        dHeader.className = 'retrieval-debug-header';
        const fusionLabel = retrievalDebug.hybrid
            ? `混合检索 · ${escapeHtml(retrievalDebug.fusion || 'rrf')} · BM25权重 ${retrievalDebug.bm25_weight}`
            : `向量检索 · ${escapeHtml(retrievalDebug.fusion || '')}`;
        dHeader.innerHTML = `
            <span class="retrieval-debug-icon">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
            </span>
            <span>检索详情 · ${fusionLabel} · 向量${retrievalDebug.vector_hits ?? 0}路/BM25 ${retrievalDebug.bm25_hits ?? 0}路 · ${retrievalDebug.elapsed_ms ?? 0}ms</span>
            <span class="agent-steps-arrow">▾</span>`;
        dHeader.onclick = () => dbg.classList.toggle('open');
        const dBody = document.createElement('div');
        dBody.className = 'retrieval-debug-body';
        const rows = (retrievalDebug.paths || []).map(p => {
            const vPart = p.vector_rank ? `向量 #${p.vector_rank} (${p.vector_score})` : '—';
            const bPart = p.bm25_rank ? `BM25 #${p.bm25_rank} (${p.bm25_score})` : '—';
            const rrf = p.rrf != null ? `<em>RRF ${p.rrf}</em>` : '';
            return `<div class="retrieval-debug-row">
                <span class="retrieval-debug-prefix">${escapeHtml(p.content_prefix || '')}</span>
                <span class="retrieval-debug-path">${vPart} · ${bPart} ${rrf}</span>
            </div>`;
        }).join('');
        dBody.innerHTML = rows || '<div class="retrieval-debug-row muted">无检索路径数据</div>';
        dbg.appendChild(dHeader);
        dbg.appendChild(dBody);
        div.appendChild(dbg);
    }

    // 反馈操作栏（v1.2：点赞/点踩，messageId 为锚点）
    if (messageId) {
        const fbBar = document.createElement('div');
        fbBar.className = 'feedback-bar';
        fbBar.innerHTML = `
            <span class="feedback-label">这个回答有帮助吗？</span>
            <button class="feedback-btn feedback-up" title="回答有帮助" onclick="submitFeedback('${messageId}','up')">👍 有帮助</button>
            <button class="feedback-btn feedback-down" title="回答不准确/缺失，可补充期望回答" onclick="submitFeedback('${messageId}','down')">👎 需改进</button>`;
        div.appendChild(fbBar);
    }

    inner.appendChild(div);
    scrollToBottom();
    return div;
}

// 提交反馈（v1.2）：点踩时弹窗补充原因与期望回答（回流评测集）
function submitFeedback(messageId, rating) {
    const reason = rating === 'down'
        ? prompt('请简要说明回答哪里不准确（可选）：\n\n提示：填写"期望回答"可回流到黄金评测集，自动纳入后续回归评测。', '')
        : '';
    if (reason === null) return; // 取消
    let expected = '';
    if (rating === 'down' && reason) {
        expected = prompt('期望的回答是什么？（可选，填写后自动回流评测集）', '');
        if (expected === null) return;
    }
    apiFetch('/api/chat/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message_id: messageId, rating, reason, expected_answer: expected }),
    }).then(resp => {
        if (!resp.ok) throw new Error('提交失败');
        return resp.json();
    }).then(() => {
        // 标记按钮已反馈
        document.querySelectorAll(`.feedback-btn`).forEach(b => b.disabled = true);
        alert(rating === 'up' ? '已记录 👍 感谢反馈！' : '已记录 👎 感谢反馈，将纳入评测回归。');
    }).catch(err => alert('反馈提交失败: ' + err.message));
}

// 管理端：用户反馈列表（v1.2）
async function loadAdminFeedback() {
    const tbody = document.getElementById('feedback-tbody');
    const rating = document.getElementById('feedback-rating-filter').value;
    try {
        const resp = await apiFetch(`/api/admin/feedback?rating=${encodeURIComponent(rating)}&limit=200`);
        if (!resp.ok) throw new Error('加载失败');
        const data = await resp.json();
        document.getElementById('feedback-count-tip').textContent = `共 ${data.total || 0} 条反馈`;
        tbody.innerHTML = '';
        if (!data.items || data.items.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="audit-empty">暂无反馈（对话消息下方可点赞/点踩）</td></tr>';
            return;
        }
        data.items.forEach(f => {
            const tr = document.createElement('tr');
            const badge = f.rating === 'up'
                ? '<span class="status-badge status-on">👍 点赞</span>'
                : '<span class="status-badge status-off">👎 点踩</span>';
            const reason = escapeHtml(f.reason || '');
            const expected = f.expected_answer
                ? `<div class="feedback-expected">期望: ${escapeHtml(String(f.expected_answer).slice(0, 80))}</div>` : '';
            tr.innerHTML = `
                <td>${escapeHtml(f._date || f.created_at || '')}</td>
                <td>${escapeHtml(f.username || '')}</td>
                <td>${badge}</td>
                <td title="${escapeHtml(f.question || '')}">${escapeHtml(String(f.question || '').slice(0, 40))}</td>
                <td class="feedback-reason">${reason}${expected}</td>`;
            tbody.appendChild(tr);
        });
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" class="audit-empty">加载失败: ${escapeHtml(err.message)}</td></tr>`;
    }
}

// 管理端：导出回流评测集（v1.2）
async function exportFeedbackDataset() {
    try {
        const resp = await apiFetch('/api/admin/feedback/export');
        if (!resp.ok) throw new Error('导出失败');
        const data = await resp.json();
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `feedback_dataset_${new Date().toISOString().slice(0, 10)}.json`;
        a.click();
        URL.revokeObjectURL(url);
        if (data.items && data.items.length > 0) {
            alert(`已导出 ${data.items.length} 条回流评测条目。\n\n可追加到 eval/dataset.json 后运行:\npython eval/run_regression.py --collect-feedback`);
        } else {
            alert('暂无可用回流条目（需要"点踩 + 填写期望回答"的反馈）');
        }
    } catch (err) {
        alert('导出失败: ' + err.message);
    }
}

// 引用高亮：点击回答中的 [n] → 高亮对应来源卡片
function highlightSource(n) {
    const cards = document.querySelectorAll('.source-chip');
    const refs = document.querySelectorAll('.md-ref');
    cards.forEach(c => c.classList.remove('active'));
    refs.forEach(r => r.classList.remove('active'));

    const target = document.querySelector(`.source-chip[data-idx="${n}"]`);
    if (target) {
        target.classList.add('active');
        target.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        // 2 秒后取消高亮
        setTimeout(() => {
            target.classList.remove('active');
            refs.forEach(r => r.classList.remove('active'));
        }, 2000);
    }
}

// 来源卡片点击：展开/收起片段
function toggleSourceCard(chip, src) {
    const existing = chip.querySelector('.source-expanded');
    document.querySelectorAll('.source-chip').forEach(c => c.classList.remove('active'));
    if (existing) {
        existing.remove();
        chip.classList.remove('active');
        return;
    }
    // 收起其他展开
    document.querySelectorAll('.source-expanded').forEach(e => e.remove());
    chip.classList.add('active');

    const expanded = document.createElement('div');
    expanded.className = 'source-expanded';
    expanded.textContent = (src.content || '').slice(0, 600);
    chip.appendChild(expanded);

    // 显示"查看全文"按钮
    const fullBtn = document.createElement('div');
    fullBtn.style.cssText = 'margin-top:6px;font-size:11px;color:var(--primary);cursor:pointer;';
    fullBtn.textContent = '↗ 查看全文';
    fullBtn.onclick = (e) => {
        e.stopPropagation();
        openPreview(src);
    };
    expanded.appendChild(fullBtn);
}

// ========== 文档预览 ==========

function openPreview(src) {
    const modal = document.getElementById('preview-modal');
    const meta = document.getElementById('preview-meta');
    const content = document.getElementById('preview-content');
    const filename = src.metadata?.filename || '未知文档';
    const collection = document.getElementById('collection-select').value;

    meta.innerHTML = `<span class="preview-file-icon">${escapeHtml(filename.slice(-3).toUpperCase())}</span>
        <span><strong>${escapeHtml(filename)}</strong></span>
        <span>· ${escapeHtml(collection)}</span>`;
    content.innerHTML = '<p class="audit-empty">加载中...</p>';
    modal.style.display = 'flex';

    apiFetch(`/api/documents/preview/${encodeURIComponent(collection)}/${encodeURIComponent(filename)}`)
        .then(resp => {
            if (!resp.ok) throw new Error('加载失败');
            return resp.json();
        })
        .then(data => {
            content.innerHTML = '';
            if (!data.chunks || data.chunks.length === 0) {
                content.innerHTML = '<p class="audit-empty">暂无内容</p>';
                return;
            }
            data.chunks.forEach((chunk, i) => {
                const sec = document.createElement('div');
                sec.className = 'preview-section';
                const head = document.createElement('div');
                head.className = 'preview-section-head';
                const type = document.createElement('span');
                type.className = 'preview-type';
                type.textContent = chunk.type || 'text';
                const idx = document.createElement('span');
                idx.textContent = `段落 ${i + 1}`;
                head.appendChild(type);
                head.appendChild(idx);
                sec.appendChild(head);
                const text = document.createElement('div');
                text.textContent = chunk.content;
                sec.appendChild(text);
                content.appendChild(sec);
            });
        })
        .catch(err => {
            content.innerHTML = `<p class="audit-empty">加载失败: ${err.message}</p>`;
        });
}

function closePreview() {
    document.getElementById('preview-modal').style.display = 'none';
}

// ========== 对话导出 ==========

function exportConversation() {
    if (!currentConversationId) {
        const msgs = document.querySelectorAll('#chat-messages .message');
        if (msgs.length === 0) {
            alert('当前没有可导出的对话内容');
            return;
        }
    }
    // 从 DOM 收集消息（保证所见即所得）
    const rows = [];
    rows.push('# 企业智库对话记录');
    rows.push('');
    rows.push(`- 导出时间: ${new Date().toLocaleString()}`);
    rows.push(`- 知识库: ${document.getElementById('collection-select').value}`);
    rows.push('');
    rows.push('---');
    rows.push('');

    document.querySelectorAll('#chat-messages .message').forEach(msg => {
        if (msg.classList.contains('user')) {
            rows.push('## 🙋 用户');
            rows.push('');
            rows.push(msg.querySelector('.message-content').textContent);
            rows.push('');
        } else if (msg.classList.contains('assistant')) {
            rows.push('## 🤖 企业智库');
            rows.push('');
            rows.push(msg.querySelector('.message-content').textContent);
            // 来源
            const chips = msg.querySelectorAll('.source-chip .source-name');
            if (chips.length) {
                rows.push('');
                rows.push('**参考来源:**');
                chips.forEach((c, i) => rows.push(`${i + 1}. ${c.textContent}`));
            }
            rows.push('');
        }
    });

    const blob = new Blob(['\uFEFF' + rows.join('\n')], { type: 'text/markdown;charset=utf-8' });
    const a = document.createElement('a');
    const title = (document.getElementById('chat-title').textContent || '对话').slice(0, 30);
    a.href = URL.createObjectURL(blob);
    a.download = `企业智库_${title}_${new Date().toISOString().slice(0, 10)}.md`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

// ========== 消息发送 ==========

// 发送消息
async function sendMessage() {
    const input = document.getElementById('chat-input');
    const message = input.value.trim();
    if (!message) return;

    const useRag = document.getElementById('rag-toggle').checked;
    const collection = document.getElementById('collection-select').value;

    // 显示用户消息
    appendUserMessage(message);
    input.value = '';
    input.style.height = 'auto';

    // 显示加载状态
    const loadingId = showTyping();

    try {
        // Agent 模式：走 Function Calling 工具调用链路 (v0.9)
        if (isAgentMode()) {
            const response = await apiFetch('/api/chat/agent', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message,
                    collection_name: collection,
                    conversation_id: currentConversationId,
                }),
            });

            removeTyping(loadingId);

            if (!response.ok) {
                const error = await response.json();
                appendAssistantMessage(`错误: ${error.detail || '请求失败'}`, []);
                return;
            }

            const data = await response.json();

            if (!currentConversationId && data.conversation_id) {
                currentConversationId = data.conversation_id;
                loadConversations();
            }
            if (data.conversation_id) {
                document.getElementById('chat-title').textContent = message.slice(0, 20) + (message.length > 20 ? '...' : '');
            }

            appendAssistantMessage(data.answer, [], data.tool_steps || [], data, data.message_id || '', data.retrieval_debug || null, data.entity_hits || []);
            return;
        }

        const response = await apiFetch('/api/chat/completions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message,
                collection_name: collection,
                use_rag: useRag,
                conversation_id: currentConversationId,
            }),
        });

        removeTyping(loadingId);

        if (!response.ok) {
            const error = await response.json();
            appendAssistantMessage(`错误: ${error.detail || '请求失败'}`, []);
            return;
        }

        const data = await response.json();

        // 更新对话 ID（首次发送时后端会创建新对话）
        if (!currentConversationId && data.conversation_id) {
            currentConversationId = data.conversation_id;
            loadConversations();
        }

        // 更新标题
        if (data.conversation_id) {
            document.getElementById('chat-title').textContent = message.slice(0, 20) + (message.length > 20 ? '...' : '');
        }

        appendAssistantMessage(data.answer, data.sources || [], [], null, data.message_id || '', data.retrieval_debug || null, data.entity_hits || []);
    } catch (err) {
        removeTyping(loadingId);
        if (err.message.includes('重新登录')) {
            showAuthOverlay();
            return;
        }
        appendAssistantMessage(`连接失败: ${err.message}\n请确认服务已启动且 Ollama 正在运行。`, []);
    }
}

// 快捷提问
function quickAsk(text) {
    const input = document.getElementById('chat-input');
    input.value = text;
    sendMessage();
}

// ========== UI 辅助 ==========

function scrollToBottom() {
    const container = document.getElementById('chat-messages');
    container.scrollTop = container.scrollHeight;
}

// 显示打字指示器
function showTyping() {
    const inner = getMessagesContainer();
    const welcome = inner.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    const div = document.createElement('div');
    div.className = 'message assistant';
    div.id = 'typing-indicator';

    const indicator = document.createElement('div');
    indicator.className = 'typing-indicator';
    indicator.innerHTML = '<span></span><span></span><span></span>';
    div.appendChild(indicator);

    inner.appendChild(div);
    scrollToBottom();
    return 'typing-indicator';
}

// 移除打字指示器
function removeTyping(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
}

// HTML 转义
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ========== 文件上传 ==========

// 上传文件
async function uploadFiles() {
    const fileInput = document.getElementById('file-input');
    const statusDiv = document.getElementById('upload-status');
    const collection = document.getElementById('collection-select').value;

    for (const file of fileInput.files) {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('collection_name', collection);

        statusDiv.innerHTML = `<p>正在上传: ${file.name}...</p>`;

        try {
            const response = await apiFetch('/api/documents/upload', {
                method: 'POST',
                body: formData,
            });

            if (!response.ok) {
                const error = await response.json();
                statusDiv.innerHTML = `<p class="upload-error">上传失败: ${error.detail}</p>`;
                continue;
            }

            const data = await response.json();
            // v1.5: 大文件走异步任务，立即返回 accepted + task_id，前端轮询状态
            if (data.status === 'accepted' && data.task_id) {
                await pollTask(data.task_id, statusDiv, file.name);
                refreshStats();
                continue;
            }
            statusDiv.innerHTML = `<p class="upload-success">${data.filename}: ${data.chunks_count} 块已入库</p>`;
            refreshStats();
        } catch (err) {
            statusDiv.innerHTML = `<p class="upload-error">上传失败: ${err.message}</p>`;
        }
    }

    fileInput.value = '';
}

// v1.5: 轮询异步任务状态（大文档后台处理）
async function pollTask(taskId, statusDiv, filename) {
    const icon = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧'];
    let frame = 0;
    for (let i = 0; i < 600; i++) {  // 最多轮询 5 分钟
        statusDiv.innerHTML = `<p>${filename}: 后台处理中 ${icon[frame % icon.length]}</p>`;
        frame++;
        await new Promise(r => setTimeout(r, 500));
        let task;
        try {
            task = await apiFetch(`/api/tasks/${taskId}`).then(r => r.json());
        } catch {
            continue;  // 网络抖动重试
        }
        if (task.status === 'success') {
            const n = task.result && task.result.chunks_count;
            statusDiv.innerHTML = `<p class="upload-success">${filename}: ${n ?? '完成'} 块已入库</p>`;
            return;
        }
        if (task.status === 'failed') {
            statusDiv.innerHTML = `<p class="upload-error">${filename}: 处理失败 — ${task.error || '未知错误'}</p>`;
            return;
        }
    }
    statusDiv.innerHTML = `<p class="upload-error">${filename}: 后台处理超时，请在任务列表查看状态</p>`;
}

// 刷新知识库统计
async function refreshStats() {
    const collection = document.getElementById('collection-select').value;
    try {
        const response = await apiFetch(`/api/documents/list/${collection}`);
        if (response.ok) {
            const data = await response.json();
            document.getElementById('chunk-count').textContent = data.total_chunks;
            document.getElementById('doc-count').textContent = data.documents.length;
        }
    } catch (err) {
        console.error('刷新统计失败:', err);
    }
    loadGraph();
}

// ========== 知识图谱面板 (v1.6) ==========

let activeGraphEntity = null;   // 当前选中实体

async function loadGraph() {
    const collection = document.getElementById('collection-select').value;
    const box = document.getElementById('graph-panel');
    if (!box) return;
    activeGraphEntity = null;

    try {
        const [statsResp, entResp] = await Promise.all([
            apiFetch(`/api/graph/stats/${collection}`),
            apiFetch(`/api/graph/entities/${collection}?limit=24`),
        ]);
        if (!statsResp.ok || !entResp.ok) {
            box.innerHTML = '<p class="graph-empty">当前知识库暂无图谱数据<br><span class="hint">上传文档后自动构建实体关系</span></p>';
            return;
        }
        const stats = await statsResp.json();
        const ents = await entResp.json();

        box.innerHTML = `<div class="graph-stats"><span>实体 <b>${stats.entities || 0}</b></span><span>关系 <b>${stats.relations || 0}</b></span></div>`;
        if (!ents.items || ents.items.length === 0) {
            box.insertAdjacentHTML('beforeend', '<p class="graph-empty">暂无实体，上传文档后自动构建</p>');
            return;
        }

        const tags = document.createElement('div');
        tags.className = 'graph-tags';
        ents.items.forEach(e => {
            const tag = document.createElement('span');
            tag.className = 'graph-tag' + (e.count >= 3 ? ' hot' : '');
            tag.textContent = e.name;
            tag.title = `${e.name} · 出现 ${e.count} 次`;
            tag.onclick = () => showGraphEntity(collection, e.name);
            tags.appendChild(tag);
        });
        box.appendChild(tags);

        const detail = document.createElement('div');
        detail.id = 'graph-detail';
        box.appendChild(detail);
    } catch (err) {
        console.error('加载图谱失败:', err);
        box.innerHTML = '<p class="graph-empty">图谱加载失败</p>';
    }
}

async function showGraphEntity(collection, entity) {
    const detail = document.getElementById('graph-detail');
    if (!detail) return;
    activeGraphEntity = entity;

    try {
        const resp = await apiFetch(`/api/graph/relations/${collection}?entity=${encodeURIComponent(entity)}&limit=15`);
        if (!resp.ok) return;
        const data = await resp.json();
        const rels = data.items || [];

        detail.innerHTML = `<div class="graph-detail-head"><span>「${escapeHtml(entity)}」关联</span>
            <span class="graph-detail-close" onclick="document.getElementById('graph-detail').innerHTML=''">&times;</span></div>`;
        if (rels.length === 0) {
            detail.insertAdjacentHTML('beforeend', '<p class="graph-empty">暂无关联关系</p>');
            return;
        }
        const ul = document.createElement('ul');
        ul.className = 'graph-rel-list';
        rels.forEach(r => {
            const li = document.createElement('li');
            const other = r.direction === 'out' ? r.target : r.source;
            li.innerHTML = `<span class="graph-rel-arrow">${r.direction === 'out' ? '→' : '←'}</span>
                <span class="graph-rel-name">${escapeHtml(other)}</span>
                <span class="graph-rel-weight">×${r.weight}</span>`;
            li.onclick = () => showGraphEntity(collection, other);
            ul.appendChild(li);
        });
        detail.appendChild(ul);
    } catch (err) {
        console.error('加载实体关系失败:', err);
    }
}

// ========== 系统管理台 (v1.1: 审计/用户/知识库配额/系统配置) ==========

function openAdminPanel(tab) {
    document.getElementById('admin-modal').style.display = 'flex';
    switchAdminTab(tab || 'audit');
}

function closeAdminPanel() {
    document.getElementById('admin-modal').style.display = 'none';
}

function switchAdminTab(tab) {
    document.querySelectorAll('.admin-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tab);
    });
    ['audit', 'users', 'kbs', 'config', 'feedback'].forEach(t => {
        document.getElementById('admin-panel-' + t).style.display = (t === tab) ? '' : 'none';
    });
    if (tab === 'users') loadAdminUsers(1);
    else if (tab === 'kbs') loadAdminKbs();
    else if (tab === 'config') loadAdminConfig();
    else if (tab === 'feedback') loadAdminFeedback();
    else loadAudit(1);
}

// ---- 审计日志 ----

async function loadAudit(page) {
    const userFilter = document.getElementById('audit-user-filter').value.trim();
    const actionFilter = document.getElementById('audit-action-filter').value;
    auditPage = Math.max(1, page);

    try {
        const params = new URLSearchParams({ page: auditPage, size: 50 });
        if (userFilter) params.set('user', userFilter);
        if (actionFilter) params.set('action', actionFilter);

        const response = await apiFetch(`/api/audit?${params.toString()}`);
        if (!response.ok) return;
        const data = await response.json();

        const tbody = document.getElementById('audit-tbody');
        if (!data.items || data.items.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="audit-empty">暂无记录</td></tr>';
        } else {
            tbody.innerHTML = data.items.map(item => `
                <tr>
                    <td>${escapeHtml(item.ts)}</td>
                    <td>${escapeHtml(item.user)}</td>
                    <td><span class="action-badge">${escapeHtml(item.action)}</span></td>
                    <td>${escapeHtml(item.target)}</td>
                    <td>${escapeHtml(item.detail)}</td>
                </tr>`).join('');
        }

        const totalPages = Math.max(1, Math.ceil(data.total / data.size));
        document.getElementById('audit-page-info').textContent =
            `第 ${data.page} / ${totalPages} 页（共 ${data.total} 条）`;
        document.getElementById('audit-prev').disabled = data.page <= 1;
        document.getElementById('audit-next').disabled = data.page >= totalPages;
    } catch (err) {
        console.error('加载审计日志失败:', err);
    }
}

function exportAudit() {
    const userFilter = document.getElementById('audit-user-filter').value.trim();
    const actionFilter = document.getElementById('audit-action-filter').value;
    const params = new URLSearchParams();
    if (userFilter) params.set('user', userFilter);
    if (actionFilter) params.set('action', actionFilter);
    const token = getToken();
    fetch(`/api/audit/export?${params.toString()}`, {
        headers: token ? { 'Authorization': `Bearer ${token}` } : {},
    }).then(resp => {
        if (!resp.ok) throw new Error('导出失败');
        return resp.blob();
    }).then(blob => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'audit_log.csv';
        a.click();
        URL.revokeObjectURL(url);
    }).catch(err => alert('导出失败: ' + err.message));
}

// ---- 用户管理 ----

async function loadAdminUsers(page) {
    const keyword = document.getElementById('admin-user-keyword').value.trim();
    adminUsersPage = Math.max(1, page);

    try {
        const params = new URLSearchParams({ page: adminUsersPage, size: 10 });
        if (keyword) params.set('keyword', keyword);
        const response = await apiFetch(`/api/admin/users?${params.toString()}`);
        if (!response.ok) return;
        const data = await response.json();

        const tbody = document.getElementById('admin-users-tbody');
        if (!data.items || data.items.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="audit-empty">暂无用户</td></tr>';
        } else {
            tbody.innerHTML = data.items.map(u => {
                const locked = u.locked_until ? ' 🔒' : '';
                const roleBadge = u.role === 'admin'
                    ? '<span class="action-badge badge-admin">管理员</span>'
                    : '<span class="action-badge">用户</span>';
                const status = u.enabled
                    ? '<span class="status-badge status-on">启用' + locked + '</span>'
                    : '<span class="status-badge status-off">禁用</span>';
                const ops = [];
                if (u.enabled) {
                    ops.push(`<button class="btn btn-mini" onclick="toggleAdminUser('${u.username}', false)">禁用</button>`);
                } else {
                    ops.push(`<button class="btn btn-mini" onclick="toggleAdminUser('${u.username}', true)">启用</button>`);
                }
                ops.push(`<button class="btn btn-mini" onclick="resetAdminPassword('${u.username}')">重置密码</button>`);
                ops.push(`<button class="btn btn-mini btn-danger" onclick="deleteAdminUser('${u.username}')">删除</button>`);
                if (u.locked_until) {
                    ops.push(`<button class="btn btn-mini" onclick="unlockAdminUser('${u.username}')">解锁</button>`);
                }
                return `<tr>
                    <td>${escapeHtml(u.username)}</td>
                    <td>${escapeHtml(u.display_name)}</td>
                    <td>${roleBadge}</td>
                    <td>${status}</td>
                    <td>${escapeHtml(u.created_at)}</td>
                    <td class="ops-cell">${ops.join('')}</td>
                </tr>`;
            }).join('');
        }

        const totalPages = Math.max(1, Math.ceil(data.total / data.size));
        document.getElementById('admin-users-page').textContent =
            `第 ${data.page} / ${totalPages} 页（共 ${data.total} 人）`;
    } catch (err) {
        console.error('加载用户列表失败:', err);
    }
}

async function createAdminUser() {
    const username = document.getElementById('admin-new-username').value.trim();
    const password = document.getElementById('admin-new-password').value;
    const display_name = document.getElementById('admin-new-display').value.trim();
    const role = document.getElementById('admin-new-role').value;
    if (!username || !password) { alert('请填写用户名和密码'); return; }

    try {
        const response = await apiFetch('/api/admin/users', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password, display_name, role }),
        });
        const data = await response.json();
        if (!response.ok) { alert(data.detail || '创建失败'); return; }
        document.getElementById('admin-new-username').value = '';
        document.getElementById('admin-new-password').value = '';
        document.getElementById('admin-new-display').value = '';
        loadAdminUsers(1);
    } catch (err) {
        alert('创建失败: ' + err.message);
    }
}

async function toggleAdminUser(username, enabled) {
    try {
        const response = await apiFetch(`/api/admin/users/${encodeURIComponent(username)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled }),
        });
        const data = await response.json();
        if (!response.ok) { alert(data.detail || '操作失败'); return; }
        loadAdminUsers(adminUsersPage);
    } catch (err) {
        alert('操作失败: ' + err.message);
    }
}

async function resetAdminPassword(username) {
    const newPassword = prompt(`为用户 ${username} 设置新密码（至少 6 位）：`);
    if (newPassword === null) return;
    if (newPassword.length < 6) { alert('密码长度至少 6 位'); return; }
    try {
        const response = await apiFetch(`/api/admin/users/${encodeURIComponent(username)}/reset-password`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ new_password: newPassword }),
        });
        const data = await response.json();
        if (!response.ok) { alert(data.detail || '操作失败'); return; }
        alert(`已重置 ${username} 的密码`);
    } catch (err) {
        alert('操作失败: ' + err.message);
    }
}

async function deleteAdminUser(username) {
    if (!confirm(`确定删除用户 ${username}？其名下知识库将转移给当前管理员。`)) return;
    try {
        const response = await apiFetch(`/api/admin/users/${encodeURIComponent(username)}`, {
            method: 'DELETE',
        });
        const data = await response.json();
        if (!response.ok) { alert(data.detail || '删除失败'); return; }
        alert(`已删除 ${username}，转移知识库 ${data.transferred_kbs} 个`);
        loadAdminUsers(adminUsersPage);
    } catch (err) {
        alert('删除失败: ' + err.message);
    }
}

async function unlockAdminUser(username) {
    try {
        const response = await apiFetch(`/api/admin/users/${encodeURIComponent(username)}/unlock`, {
            method: 'POST',
        });
        const data = await response.json();
        if (!response.ok) { alert(data.detail || '操作失败'); return; }
        loadAdminUsers(adminUsersPage);
    } catch (err) {
        alert('操作失败: ' + err.message);
    }
}

// ---- 知识库配额 ----

async function loadAdminKbs() {
    try {
        const response = await apiFetch('/api/admin/knowledge-bases');
        if (!response.ok) return;
        const data = await response.json();
        const tbody = document.getElementById('admin-kbs-tbody');
        if (!data.collections || data.collections.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="audit-empty">暂无知识库</td></tr>';
            return;
        }
        tbody.innerHTML = data.collections.map(kb => {
            const chunks = `${kb.chunk_count} / <input type="number" class="quota-input" id="quota-chunks-${escapeHtml(kb.name)}" value="${kb.quota_chunks}" title="-1 不限制">`;
            const docs = `${kb.document_count} / <input type="number" class="quota-input" id="quota-docs-${escapeHtml(kb.name)}" value="${kb.quota_documents}" title="-1 不限制">`;
            return `<tr>
                <td>${escapeHtml(kb.name)}</td>
                <td>${escapeHtml(kb.owner)}</td>
                <td>${chunks}</td>
                <td>${docs}</td>
                <td>${escapeHtml(kb.created_at)}</td>
                <td class="ops-cell">
                    <button class="btn btn-mini" onclick="saveKbQuota('${escapeHtml(kb.name)}')">保存配额</button>
                </td>
            </tr>`;
        }).join('');
    } catch (err) {
        console.error('加载知识库配额失败:', err);
    }
}

async function saveKbQuota(name) {
    const quota_chunks = parseInt(document.getElementById('quota-chunks-' + name).value, 10);
    const quota_documents = parseInt(document.getElementById('quota-docs-' + name).value, 10);
    try {
        const response = await apiFetch(`/api/admin/knowledge-bases/${encodeURIComponent(name)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ quota_chunks, quota_documents }),
        });
        const data = await response.json();
        if (!response.ok) { alert(data.detail || '保存失败'); return; }
        loadAdminKbs();
    } catch (err) {
        alert('保存失败: ' + err.message);
    }
}

// ---- 系统配置 ----

const CONFIG_FIELDS = [
    { section: 'llm', key: 'provider', label: 'LLM 提供商', type: 'select', options: ['ollama', 'openai'] },
    { section: 'llm', key: 'model', label: 'LLM 模型', type: 'text' },
    { section: 'llm', key: 'ollama_base_url', label: 'Ollama 地址', type: 'text' },
    { section: 'llm', key: 'openai_base_url', label: 'OpenAI 地址', type: 'text' },
    { section: 'llm', key: 'temperature', label: '温度 (0-1)', type: 'number' },
    { section: 'llm', key: 'max_tokens', label: '最大输出 Token', type: 'number' },
    { section: 'llm', key: 'openai_api_key', label: 'OpenAI API Key', type: 'password' },
    { section: 'embedding', key: 'provider', label: '嵌入提供商', type: 'select', options: ['ollama', 'openai'] },
    { section: 'embedding', key: 'model', label: '嵌入模型', type: 'text' },
    { section: 'retrieval', key: 'top_k', label: '检索 Top-K', type: 'number' },
    { section: 'retrieval', key: 'score_threshold', label: '相似度阈值', type: 'number', step: '0.01' },
    { section: 'retrieval', key: 'hybrid_search', label: '混合检索', type: 'select', options: ['true', 'false'] },
    { section: 'retrieval', key: 'bm25_weight', label: 'BM25 权重', type: 'number', step: '0.01' },
    { section: 'reranker', key: 'enabled', label: '重排启用', type: 'select', options: ['true', 'false'] },
    { section: 'reranker', key: 'top_n', label: '重排 Top-N', type: 'number' },
    { section: 'query_rewrite', key: 'enabled', label: 'Query 改写', type: 'select', options: ['true', 'false'] },
    { section: 'agent', key: 'max_iterations', label: 'Agent 最大轮数', type: 'number' },
    { section: 'document', key: 'chunk_size', label: '分块大小', type: 'number' },
    { section: 'document', key: 'chunk_overlap', label: '分块重叠', type: 'number' },
    { section: 'document', key: 'ocr_enabled', label: 'PDF OCR', type: 'select', options: ['true', 'false'] },
];

async function loadAdminConfig() {
    try {
        const response = await apiFetch('/api/admin/config');
        if (!response.ok) return;
        adminConfig = (await response.json()).config;
        const form = document.getElementById('config-form');
        form.innerHTML = CONFIG_FIELDS.map((f, i) => {
            const val = (adminConfig[f.section] || {})[f.key];
            const label = `<label class="config-label" for="cfg-${i}">${f.label}</label>`;
            let input;
            if (f.type === 'select') {
                const opts = f.options.map(o =>
                    `<option value="${o}" ${String(val) === o ? 'selected' : ''}>${o}</option>`).join('');
                input = `<select class="filter-input config-input" id="cfg-${i}">${opts}</select>`;
            } else if (f.type === 'password') {
                input = `<input class="filter-input config-input" id="cfg-${i}" type="password" placeholder="${val === '****' ? '****（留空保留原值）' : ''}">`;
            } else if (f.type === 'text') {
                input = `<input class="filter-input config-input" id="cfg-${i}" type="text" value="${escapeHtml(String(val))}">`;
            } else {
                input = `<input class="filter-input config-input" id="cfg-${i}" type="number" step="${f.step || '1'}" value="${val}">`;
            }
            return `<div class="config-row">${label}${input}</div>`;
        }).join('');
    } catch (err) {
        console.error('加载配置失败:', err);
    }
}

async function saveAdminConfig() {
    if (!adminConfig) return;
    const patch = {};
    CONFIG_FIELDS.forEach((f, i) => {
        const el = document.getElementById(`cfg-${i}`);
        if (!el) return;
        let value;
        if (f.type === 'select') {
            value = el.value === 'true' ? true : (el.value === 'false' ? false : el.value);
        } else if (f.type === 'password') {
            if (el.value) value = el.value;  // 留空 = 保留原值
        } else if (f.type === 'text') {
            value = el.value;
        } else {
            value = Number(el.value);
        }
        if (value !== undefined) {
            if (!patch[f.section]) patch[f.section] = {};
            patch[f.section][f.key] = value;
        }
    });
    try {
        const response = await apiFetch('/api/admin/config', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(patch),
        });
        const data = await response.json();
        const msg = document.getElementById('config-msg');
        if (!response.ok) {
            msg.textContent = '保存失败: ' + (data.detail || '');
            msg.className = 'config-msg config-err';
            return;
        }
        adminConfig = data.config;
        msg.textContent = '已保存并生效';
        msg.className = 'config-msg config-ok';
        loadAdminConfig();
        setTimeout(() => { msg.textContent = ''; }, 3000);
    } catch (err) {
        document.getElementById('config-msg').textContent = '保存失败: ' + err.message;
    }
}

// ========== 事件绑定 ==========

// 键盘事件
function handleKeyDown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
    }
}

// 自动调整输入框高度
document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('chat-input');
    input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 130) + 'px';
    });

    // 拖拽上传
    const uploadArea = document.getElementById('upload-area');
    if (uploadArea) {
        uploadArea.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadArea.style.borderColor = 'var(--primary)';
            uploadArea.style.background = 'var(--gradient-soft)';
        });
        uploadArea.addEventListener('dragleave', () => {
            uploadArea.style.borderColor = '';
            uploadArea.style.background = '';
        });
        uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadArea.style.borderColor = '';
            uploadArea.style.background = '';
            const files = e.dataTransfer.files;
            document.getElementById('file-input').files = files;
            uploadFiles();
        });
    }

    // 知识库切换时刷新统计
    document.getElementById('collection-select').addEventListener('change', refreshStats);

    // 初始检查登录态（先探测认证是否开启，支持内网免登录模式）
    (async function boot() {
        try {
            const resp = await fetch(`${API_BASE}/api/auth/status`);
            if (resp.ok) {
                const status = await resp.json();
                if (status.auth_enabled === false) {
                    renderUserInfo({ username: 'system', role: 'admin', display_name: '系统(免认证)', is_admin: true });
                    updateThemeIcon();
                    showApp();
                    await finishBootstrap();
                    return;
                }
            }
        } catch (e) { /* 状态接口不可达时按 token 兜底 */ }
        if (getToken()) {
            initApp();
        } else {
            showAuthOverlay();
        }
    })();
});
