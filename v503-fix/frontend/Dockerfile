FROM node:20-alpine

WORKDIR /app

# Copy package files
COPY package.json ./
# Install without lock (build context doesn't include package-lock)
RUN npm install --legacy-peer-deps

# Copy source
COPY . .

# Build
RUN npm run build

# Expose preview port
EXPOSE 5173

CMD ["npm", "run", "preview"]
