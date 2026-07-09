import { createRouter, createWebHistory } from 'vue-router'
import ThreadReaderView from './views/ThreadReaderView.vue'
import AdminView from './views/AdminView.vue'

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
      redirect: '/admin/databases',
    },
    {
      path: '/admin',
      component: AdminView,
      redirect: '/admin/post-versions',
      children: [
        {
          path: 'post-versions',
          component: () => import('./views/PostVersionAdminView.vue'),
        },
        {
          path: 'databases',
          component: () => import('./views/DatabaseView.vue'),
        },
      ],
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/threads',
    },
  ],
})

export default router
