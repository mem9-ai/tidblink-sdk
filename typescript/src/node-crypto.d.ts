// The one Node.js API the SDK uses beyond the Web platform: hashing bytes in place.
declare module "node:crypto" {
  interface Hash {
    update(data: Uint8Array): Hash;
    digest(): Uint8Array;
  }
  export function createHash(algorithm: "sha256"): Hash;
}
