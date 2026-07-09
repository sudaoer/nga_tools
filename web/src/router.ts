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
      redirect: (to) => ({
        path: '/admin/databases',
        query: to.query,
      }),
    },
    {
      path: '/admin',
      component: AdminView,
      redirect: (to) => ({
        path: '/admin/post-versions',
        query: to.query,
      }),
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
