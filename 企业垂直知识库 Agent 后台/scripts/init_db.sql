-- =====================================================================
-- 企业垂直知识库 Agent 后台 —— RBAC 鉴权模块建表脚本（本模块范围）
-- 目标库：MySQL 8.0+ / utf8mb4 / InnoDB
-- 说明：本脚本仅包含当前模块（用户 + 审计日志）所需的表；
--       其余业务表见 docs/需求分析与设计.md 第 1 节，随后续模块补充。
-- =====================================================================

CREATE DATABASE IF NOT EXISTS kb_agent
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;
USE kb_agent;

-- ---------------------------------------------------------------------
-- 1. 用户表（仅两种角色，简化 RBAC）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  username      VARCHAR(64)     NOT NULL COMMENT '登录名',
  password_hash VARCHAR(255)    NOT NULL COMMENT 'bcrypt 密码哈希',
  display_name  VARCHAR(64)     DEFAULT NULL COMMENT '显示名',
  email         VARCHAR(128)    DEFAULT NULL COMMENT '邮箱',
  role          ENUM('admin','user') NOT NULL DEFAULT 'user' COMMENT '角色：admin=管理员 user=普通用户',
  status        ENUM('active','disabled') NOT NULL DEFAULT 'active' COMMENT '账号状态',
  created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_users_username (username),
  KEY idx_users_role (role),
  KEY idx_users_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户表';

-- ---------------------------------------------------------------------
-- 2. 操作审计日志表（管理员关键操作留痕）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS operation_logs (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id     BIGINT UNSIGNED DEFAULT NULL,
  action      VARCHAR(64)     NOT NULL COMMENT '动作：create_user/update_user/delete_user/reset_password...',
  target_type VARCHAR(32)     DEFAULT NULL COMMENT '对象类型',
  target_id   VARCHAR(64)     DEFAULT NULL COMMENT '对象ID',
  detail      JSON            DEFAULT NULL COMMENT '附加信息',
  ip          VARCHAR(64)     DEFAULT NULL,
  created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_oplog_user (user_id),
  KEY idx_oplog_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='操作审计日志';

-- ---------------------------------------------------------------------
-- 3. 知识库表
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_bases (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name             VARCHAR(128)    NOT NULL COMMENT '知识库名称',
  description      VARCHAR(512)    DEFAULT NULL COMMENT '描述',
  owner_id         BIGINT UNSIGNED NOT NULL COMMENT '创建者 users.id',
  embedding_model  VARCHAR(64)     NOT NULL DEFAULT 'default' COMMENT 'Embedding 模型标识',
  chunk_size       INT             NOT NULL DEFAULT 500 COMMENT '分块大小(字符)',
  chunk_overlap    INT             NOT NULL DEFAULT 50 COMMENT '分块重叠(字符)',
  top_k            INT             NOT NULL DEFAULT 5 COMMENT '默认召回条数',
  rerank_threshold FLOAT           NOT NULL DEFAULT 0.0 COMMENT '重排序过滤阈值',
  status           ENUM('active','archived') NOT NULL DEFAULT 'active',
  doc_count        INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '文档数(冗余统计)',
  created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_kbs_owner (owner_id),
  KEY idx_kbs_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='知识库表';

-- ---------------------------------------------------------------------
-- 4. 文档表
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  kb_id        BIGINT UNSIGNED NOT NULL COMMENT '所属知识库',
  title        VARCHAR(255)    NOT NULL COMMENT '文档标题/文件名',
  file_type    ENUM('pdf','docx','xlsx','md','txt') NOT NULL COMMENT '文件类型',
  file_path    VARCHAR(512)    NOT NULL COMMENT '存储路径',
  file_size    BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '字节数',
  file_hash    CHAR(64)        DEFAULT NULL COMMENT 'SHA256，用于去重',
  parse_status ENUM('pending','parsing','chunking','embedding','done','failed') NOT NULL DEFAULT 'pending',
  chunk_count  INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '已生成分块数',
  error_msg    VARCHAR(512)    DEFAULT NULL COMMENT '失败原因',
  uploaded_by  BIGINT UNSIGNED NOT NULL COMMENT '上传人 users.id',
  created_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_docs_kb (kb_id),
  KEY idx_docs_status (parse_status),
  KEY idx_docs_hash (file_hash),
  CONSTRAINT fk_docs_kb FOREIGN KEY (kb_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='文档表';

-- ---------------------------------------------------------------------
-- 5. 文档分块表（BM25 检索在应用内实现，故此处不建 FULLTEXT 索引）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_chunks (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  doc_id       BIGINT UNSIGNED NOT NULL COMMENT '所属文档',
  kb_id        BIGINT UNSIGNED NOT NULL COMMENT '所属知识库(冗余便于过滤)',
  chunk_index  INT UNSIGNED    NOT NULL COMMENT '块序号',
  content      MEDIUMTEXT      NOT NULL COMMENT '块原文',
  token_count  INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '估算 token 数',
  metadata     JSON            DEFAULT NULL COMMENT '来源信息',
  chroma_id    VARCHAR(128)    DEFAULT NULL COMMENT 'Chroma 中向量ID',
  created_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_chunk_chroma (chroma_id),
  KEY idx_chunks_doc (doc_id),
  KEY idx_chunks_kb (kb_id),
  CONSTRAINT fk_chunks_doc FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE,
  CONSTRAINT fk_chunks_kb  FOREIGN KEY (kb_id)  REFERENCES knowledge_bases(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='文档分块表';

-- ---------------------------------------------------------------------
-- 6. 对话会话表
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id    BIGINT UNSIGNED NOT NULL COMMENT '发起人 users.id',
  kb_id      BIGINT UNSIGNED DEFAULT NULL COMMENT '绑定的主知识库(可空)',
  title      VARCHAR(255)    DEFAULT NULL COMMENT '会话标题(首问摘要)',
  created_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_conv_user (user_id),
  KEY idx_conv_kb (kb_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='对话会话表';

-- ---------------------------------------------------------------------
-- 7. 消息表（多轮对话历史）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  conversation_id BIGINT UNSIGNED NOT NULL COMMENT '所属会话',
  user_id         BIGINT UNSIGNED NOT NULL COMMENT '发言用户 users.id',
  role            ENUM('user','assistant','system','tool') NOT NULL COMMENT '消息角色',
  content         MEDIUMTEXT      NOT NULL COMMENT '消息正文',
  agent_route     ENUM('rag','db_query','report','direct') DEFAULT NULL COMMENT 'Agent 最终路由',
  trace_id        CHAR(36)        DEFAULT NULL COMMENT '关联 Trace',
  input_tokens    INT UNSIGNED    NOT NULL DEFAULT 0,
  output_tokens   INT UNSIGNED    NOT NULL DEFAULT 0,
  latency_ms      INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '本消息总耗时(ms)',
  status          ENUM('running','done','failed') NOT NULL DEFAULT 'running',
  error_msg       VARCHAR(512)    DEFAULT NULL,
  sources         JSON            DEFAULT NULL COMMENT '引用来源快照（回答内 [来源i]/《文档》第N块 的溯源目标）',
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_msg_conv (conversation_id),
  KEY idx_msg_user (user_id),
  KEY idx_msg_trace (trace_id),
  KEY idx_msg_route (agent_route),
  CONSTRAINT fk_msg_conv FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='消息表';

-- 提示：管理员账号请通过 scripts/seed.py 初始化（密码经 bcrypt 哈希后写入），
--       不要在此处用明文密码插入。
