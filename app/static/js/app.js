/**
 * 企业智库 RAG 问答系统 v0.8
 * 多轮对话 + 认证 + 知识库隔离 + 高级 UI（Markdown/来源卡片/文档预览/导出/暗黑模式）
 */

const API_BASE = '';
let currentConversationId = null;
let currentUser = null;
let auditPage = 1;
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
    // 管理员显示审计按钮
    document.getElementById('btn-audit').style.display = user.is_admin ? '' : 'none';
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
                appendAssistantMessage(msg.content, msg.sources || []);
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

function appendAssistantMessage(content, sources) {
    const inner = getMessagesContainer();
    const welcome = inner.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    const div = document.createElement('div');
    div.className = 'message assistant';

    const bubble = document.createElement('div');
    bubble.className = 'message-content';

    // Markdown 渲染（替换回答中的 [来源: xxx] 标记为纯文本，避免干扰）
    const clean = content.replace(/\[来源:\s*[^\]]*\]/g, '');
    renderMarkdownBlocks(clean).forEach(el => bubble.appendChild(el));
    div.appendChild(bubble);

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

    inner.appendChild(div);
    scrollToBottom();
    return div;
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

        appendAssistantMessage(data.answer, data.sources || []);
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
            statusDiv.innerHTML = `<p class="upload-success">${data.filename}: ${data.chunks_count} 块已入库</p>`;
            refreshStats();
        } catch (err) {
            statusDiv.innerHTML = `<p class="upload-error">上传失败: ${err.message}</p>`;
        }
    }

    fileInput.value = '';
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
}

// ========== 审计日志（管理员） ==========

function openAuditPanel() {
    auditPage = 1;
    document.getElementById('audit-modal').style.display = 'flex';
    loadAudit(1);
}

function closeAuditPanel() {
    document.getElementById('audit-modal').style.display = 'none';
}

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
