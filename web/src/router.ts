import { createRouter, createWebHistory } from 'vue-router'
import ThreadReaderView from './views/ThreadReaderView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: (to) => ({
        path: '/threads',
        query: to.query,
      }),
    },
    {
      path: '/threads',
      component: ThreadReaderView,
    },
    {
      path: '/databases',
      component: () => import('./views/DatabaseView.vue'),
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/threads',
    },
  ],
})

export default router
