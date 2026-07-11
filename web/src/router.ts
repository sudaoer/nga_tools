import { createRouter, createWebHistory } from 'vue-router'

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
      component: () => import('./views/ThreadReaderView.vue'),
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
      component: () => import('./views/AdminView.vue'),
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
        {
          path: 'image-usage',
          component: () => import('./views/ImageUsageAdminView.vue'),
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
