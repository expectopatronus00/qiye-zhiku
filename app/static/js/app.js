/**
 * 企业智库 RAG 问答系统 v0.2 - 多轮对话支持
 */

const API_BASE = '';
let currentConversationId = null;

// ========== 对话管理 ==========

// 加载对话列表
async function loadConversations() {
    try {
        const response = await fetch(`${API_BASE}/api/chat/conversations`);
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
        const response = await fetch(`${API_BASE}/api/chat/conversations`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ collection_name: collection }),
        });
        if (!response.ok) return;
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
        const response = await fetch(`${API_BASE}/api/chat/conversations/${conversationId}`);
        if (!response.ok) return;
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
        const response = await fetch(`${API_BASE}/api/chat/conversations/${conversationId}`, {
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
        const response = await fetch(`${API_BASE}/api/chat/completions`, {
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
            const response = await fetch(`${API_BASE}/api/documents/upload`, {
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
        const response = await fetch(`${API_BASE}/api/documents/list/${collection}`);
        if (response.ok) {
            const data = await response.json();
            document.getElementById('chunk-count').textContent = data.total_chunks;
            document.getElementById('doc-count').textContent = data.documents.length;
        }
    } catch (err) {
        console.error('刷新统计失败:', err);
    }
}

// 创建新知识库
async function createCollection() {
    const name = prompt('请输入新知识库名称:');
    if (!name) return;

    try {
        const response = await fetch(`${API_BASE}/api/knowledge/collections/${name}`, {
            method: 'POST',
        });
        if (response.ok) {
            const select = document.getElementById('collection-select');
            const option = document.createElement('option');
            option.value = name;
            option.textContent = name;
            option.selected = true;
            select.appendChild(option);
            alert(`知识库 "${name}" 创建成功`);
        }
    } catch (err) {
        alert('创建失败: ' + err.message);
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

    // 初始加载
    loadConversations();
    refreshStats();
});
