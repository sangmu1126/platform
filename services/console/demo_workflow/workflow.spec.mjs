import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test, expect } from "@playwright/test";

test("Sova full local workflow stops for approvals, records one effect, survives refresh", async ({
  page,
}, info) => {
  const access = JSON.parse(
    readFileSync(
      join(info.config.metadata.demoDirectory, "access.private.json"),
    ),
  );
  const errors = [];
  const external = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1") {
      external.push(url.origin);
      return route.abort();
    }
    return route.continue();
  });
  page.on("dialog", (dialog) => dialog.accept());
  await page.goto(access.bootstrap_url);
  await expect(page.locator("#demo-current")).toContainText("조사");
  await expect(page.locator("#demo-workflow")).toContainText("합성 실행");
  await expect(page.locator(".sidebar")).toHaveCount(1);
  await page
    .getByRole("button", { name: "승인 전까지 진행", exact: true })
    .click();
  await expect(page.locator("#demo-current")).toContainText("리허설");
  await expect(page.locator("#demo-next")).toHaveText("변경 승인");
  await expect(page.locator("#demo-auto")).toBeDisabled();
  await page.locator("#demo-next").click();
  await expect(page.locator("#demo-next")).toHaveText("다음 단계");
  await page.locator("#demo-auto").click();
  await expect(page.locator("#demo-current")).toContainText("전환 시작 승인");
  await expect(page.locator("#demo-gate li")).toHaveCount(3);
  await page.locator("#demo-next").click();
  await expect(page.locator("#demo-next")).toHaveText("다음 단계");
  await page.locator("#demo-next").click();
  await expect(page.locator("#demo-next")).toHaveText("컷오버 승인");
  await page.locator("#demo-next").click();
  await expect(page.locator("#demo-next")).toHaveText("데모 전환 실행");
  await page.locator("#demo-next").click();
  await expect(page.locator("#demo-current")).toContainText("전환 ·");
  await page.locator("#demo-auto").click();
  await expect(page.locator("#demo-current")).toContainText("데모 완료");
  await expect(page.locator("#demo-current")).toContainText("합성 효과 1회");
  await page.reload();
  await expect(page.locator("#demo-current")).toContainText("데모 완료");
  await expect(page.locator("#demo-next")).toBeDisabled();
  await expect(page.locator("#state")).toHaveText("완료", { timeout: 20_000 });
  await expect(page.locator("#connection")).toContainText("서버 이벤트 연결됨");
  await page.screenshot({
    path: info.outputPath("workflow-complete.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
  expect(external).toEqual([]);
});
