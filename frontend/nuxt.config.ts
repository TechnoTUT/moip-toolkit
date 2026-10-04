// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2024-11-01',
  devtools: { enabled: false },
  modules: ['@nuxt/ui'],
  ssr: false, // Pure SPA mode
  colorMode: {
    preference: 'light',
    classSuffix: ''
  },
  tailwindcss: {
    config: {
      theme: {
        extend: {
          colors: {
            brand: {
              DEFAULT: '#C7000A',
              hover: '#b00009',
              light: '#e61e29',
              dim: '#590509'
            }
          }
        }
      }
    }
  },
  app: {
    head: {
      title: 'MoIP Control',
      meta: [
        { charset: 'utf-8' },
        { name: 'viewport', content: 'width=device-width, initial-scale=1' }
      ],
      link: [
        { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' }
      ]
    }
  },
  nitro: {
    preset: 'static',
    output: {
      publicDir: '.output/public'
    }
  }
})
