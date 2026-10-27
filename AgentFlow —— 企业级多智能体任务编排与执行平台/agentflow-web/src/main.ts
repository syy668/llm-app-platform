import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ArcoVue from '@arco-design/web-vue'
import ArcoVueIcon from '@arco-design/web-vue/es/icon'
import '@arco-design/web-vue/dist/arco.css'

import App from './App.vue'
import { router } from './router'
import './styles/global.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ArcoVue, {
  // 全局默认尺寸：B 端后台用 small 提升信息密度
  size: 'medium',
})
app.use(ArcoVueIcon)

app.mount('#app')
