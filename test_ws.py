import asyncio
async def test():
    process = await asyncio.create_subprocess_exec(
        "pio", "run", "-t", "upload",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd="../firmware/pico_robot"
    )
    stdout, _ = await process.communicate()
    print(stdout.decode())
asyncio.run(test())
