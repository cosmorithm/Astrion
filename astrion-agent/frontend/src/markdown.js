import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'

const markdown = new MarkdownIt({ html: false, breaks: true, linkify: true })

// 报告中的链接由用户主动打开；图片只展示说明，不自动加载外部地址。
const defaultLink = markdown.renderer.rules.link_open || ((tokens, index, options, env, self) => self.renderToken(tokens, index, options))
markdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
  tokens[index].attrSet('target', '_blank')
  tokens[index].attrSet('rel', 'noopener noreferrer')
  return defaultLink(tokens, index, options, env, self)
}
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(tokens[index].content)

export function renderMarkdown(text) {
  return DOMPurify.sanitize(markdown.render(text || ''), {
    ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 's', 'blockquote', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      'ul', 'ol', 'li', 'pre', 'code', 'hr', 'a', 'table', 'thead', 'tbody', 'tr', 'th', 'td'],
    ALLOWED_ATTR: ['href', 'title', 'target', 'rel', 'class', 'start'],
  })
}
