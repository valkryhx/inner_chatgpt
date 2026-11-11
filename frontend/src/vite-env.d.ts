/// <reference types="vite/client" />

// Vite 的静态资源导入（import icon from './x.png'）需要模块声明，
// 否则 tsc 会报 TS2307。vite/client 已内置这部分；这个文件的作用
// 是确保它被 tsconfig 的 include 覆盖到。
declare module '*.png' {
  const src: string
  export default src
}
declare module '*.jpg' {
  const src: string
  export default src
}
declare module '*.svg' {
  const src: string
  export default src
}