/**
 * 企业智库 RAG 问答系统 v0.7 - 多轮对话 + 多用户认证 + 知识库权限隔离
 */

const API_BASE = '';
let currentConversationId = null;
let currentUser = null;
let auditPage = 1;

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

// 启动：校验令牌
async function initApp() {
    try {
        const response = await apiFetch('/api/auth/me');
        if (!response.ok) throw new Error('auth failed');
        currentUser = await response.json();
    } catch (err) {
        showAuthOverlay();
        return;
    }

    // 渲染用户信息
    const avatar = document.getElementById('user-avatar');
    avatar.textContent = currentUser.display_name ? currentUser.display_name[0] : currentUser.username[0];
    document.getElementById('user-name').textContent =
        currentUser.display_name || currentUser.username;
    const roleEl = document.getElementById('user-role');
    roleEl.textContent = currentUser.is_admin ? '管理员' : '用户';
    roleEl.className = 'user-role ' + (currentUser.is_admin ? 'role-admin' : 'role-user');

    // 管理员显示审计按钮
    document.getElementById('btn-audit').style.display = currentUser.is_admin ? '' : 'none';

    showApp();
    await loadCollections();
    await loadConversations();
    await refreshStats();
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
        empty.textContent = '暂无对话';
        list.appendChild(empty);
        return;
    }
    conversations.forEach(conv => {
        const item = document.createElement('div');
        item.className = 'conv-item' + (conv.id === currentConversationId ? ' active' : '');
        item.dataset.id = conv.id;
        item.innerHTML = `
            <span class="conv-title">${escapeHtml(conv.title)}</span>
            <span class="conv-meta">${conv.message_count} 条消息</span>
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

        // 恢复消息
        (data.messages || []).forEach(msg => {
            if (msg.role === 'user' || msg.role === 'assistant') {
                let content = msg.content;
                // 添加来源信息
                if (msg.sources && msg.sources.length > 0) {
                    const sources = msg.sources
                        .map(s => `  - ${s.metadata?.filename || '未知'} (相关度: ${(s.score * 100).toFixed(0)}%)`)
                        .join('\n');
                    content += `\n\n---\n参考来源:\n${sources}`;
                }
                appendMessage(msg.role, content);
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

// ========== 消息发送 ==========

// 发送消息
async function sendMessage() {
    const input = document.getElementById('chat-input');
    const message = input.value.trim();
    if (!message) return;

    const useRag = document.getElementById('rag-toggle').checked;
    const collection = document.getElementById('collection-select').value;

    // 显示用户消息
    appendMessage('user', message);
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
            appendMessage('assistant', `错误: ${error.detail || '请求失败'}`);
            return;
        }

        const data = await response.json();

        // 更新对话 ID（首次发送时后端会创建新对话）
        if (!currentConversationId && data.conversation_id) {
            currentConversationId = data.conversation_id;
            loadConversations();
        }

        let content = data.answer;

        // 添加来源信息
        if (data.sources && data.sources.length > 0) {
            const sources = data.sources
                .map(s => `  - ${s.metadata?.filename || '未知'} (相关度: ${(s.score * 100).toFixed(0)}%)`)
                .join('\n');
            content += `\n\n---\n参考来源:\n${sources}`;
        }

        appendMessage('assistant', content);
    } catch (err) {
        removeTyping(loadingId);
        if (err.message.includes('重新登录')) {
            showAuthOverlay();
            return;
        }
        appendMessage('assistant', `连接失败: ${err.message}\n请确认服务已启动且 Ollama 正在运行。`);
    }
}

// ========== UI 辅助 ==========

// 清空聊天区域
function clearChatArea() {
    const container = document.getElementById('chat-messages');
    container.innerHTML = '<div class="welcome-message"><h3>企业智库</h3><p>上传文档到知识库，然后开始提问。</p><p>支持多轮对话，上下文自动关联。</p></div>';
}

// 添加消息到界面
function appendMessage(role, content) {
    const container = document.getElementById('chat-messages');

    // 移除欢迎消息
    const welcome = container.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    const div = document.createElement('div');
    div.className = `message ${role}`;

    const bubble = document.createElement('div');
    bubble.className = 'message-content';
    bubble.textContent = content;
    div.appendChild(bubble);

    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

// 显示打字指示器
function showTyping() {
    const container = document.getElementById('chat-messages');

    // 移除欢迎消息
    const welcome = container.querySelector('.welcome-message');
    if (welcome) welcome.remove();

    const div = document.createElement('div');
    div.className = 'message assistant';
    div.id = 'typing-indicator';

    const indicator = document.createElement('div');
    indicator.className = 'typing-indicator';
    indicator.innerHTML = '<span></span><span></span><span></span>';
    div.appendChild(indicator);

    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
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
            statusDiv.innerHTML = `<p class="upload-success">${data.filename}: ${data.chunks_count} 个文档块已入库</p>`;
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
                    <td>${escapeHtml(item.action)}</td>
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
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });

    // 拖拽上传
    const uploadArea = document.getElementById('upload-area');
    if (uploadArea) {
        uploadArea.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadArea.style.borderColor = 'var(--primary)';
            uploadArea.style.background = 'rgba(26, 115, 232, 0.08)';
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

    // 初始检查登录态
    if (getToken()) {
        initApp();
    } else {
        showAuthOverlay();
    }
});
