<script setup lang="ts">
/**
 * 应用根组件：只负责主题与语言环境，布局由路由决定。
 * 登录页不带侧边栏，业务页统一走 BasicLayout。
 */
import zhCN from '@arco-design/web-vue/es/locale/lang/zh-cn'
</script>

<template>
  <a-config-provider :locale="zhCN">
    <router-view />
  </a-config-provider>
</template>
