import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import basicSsl from "@vitejs/plugin-basic-ssl";

const useHttps = process.env.VITE_HTTPS !== "false";

export default defineConfig({
  plugins: useHttps ? [react(), basicSsl()] : [react()],
  resolve: {
    dedupe: ["react", "react-dom"],
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api/v1/realtime": {
        target: "ws://127.0.0.1:8000",
        ws: true,
        changeOrigin: true,
      },
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
